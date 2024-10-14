import copy
import time
from functools import partial
from typing import Any, Dict, Tuple

import chex
import flax
import hydra
import jax
import jax.numpy as jnp
import numpy as np
import optax
from colorama import Fore, Style
from flax.core.frozen_dict import FrozenDict
from omegaconf import DictConfig, OmegaConf
from optax._src.base import OptState
from rich.pretty import pprint
from typing_extensions import NamedTuple

from jumanji.env import Environment
from mava.networks.ff_rnn_network import FeedForwardActor as Actor
from mava.networks.sable_network import SableNetwork
from mava.systems.sable.types import (
    ActorApply,
    CriticApply,
    ExperimentOutput,
    LearnerFn,
    OptStates,
    Params,
    PPOTransition,
    State,
    TimeStep,
    ValueNormParams,
)
from mava.utils import make_env as environments
from mava.utils.checkpointing import Checkpointer
from mava.utils.jax_utils import unreplicate_n_dims
from mava.utils.logger import LogEvent, MavaLogger
from mava.utils.sable_utils import concat_time_and_agents, get_init_hidden_state
from mava.utils.total_timestep_checker import check_total_timesteps
from mava.utils.training import make_learning_rate
from mava.utils.value_norm import denormalise, normalise, update_running_mean_var
from mava.wrappers.episode_metrics import get_final_step_metrics

# jax.config.update("jax_debug_nans", True)


class LearnerState(NamedTuple):
    """State of the learner."""

    params: Params
    opt_states: OptStates
    key: chex.PRNGKey
    env_state: State
    timestep: TimeStep
    value_norm_params: ValueNormParams
    hidden_state: chex.Array


def huber_loss(
    value: chex.Array, target: chex.Array, delta: float = 10.0
) -> chex.Array:
    """Huber loss."""
    error = jnp.abs(target - value)
    return jnp.where(error <= delta, 0.5 * error**2, delta * (error - 0.5 * delta))


def get_learner_fn(
    env: Environment,
    apply_fns: Tuple[ActorApply, CriticApply],
    update_fns: Tuple[optax.TransformUpdateFn, optax.TransformUpdateFn],
    config: DictConfig,
) -> LearnerFn[LearnerState]:
    """Get the learner function."""

    # Get apply and update functions for actor and critic networks.
    actor_action_select_fn, actor_apply_fn = apply_fns
    actor_update_fn, _ = update_fns

    def _update_step(learner_state: LearnerState, _: Any) -> Tuple[LearnerState, Tuple]:
        """A single update of the network.

        This function steps the environment and records the trajectory batch for
        training. It then calculates advantages and targets based on the recorded
        trajectory and updates the actor and critic networks based on the calculated
        losses.

        Args:
            learner_state (NamedTuple):
                - params (Params): The current model parameters.
                - opt_states (OptStates): The current optimizer states.
                - key (PRNGKey): The random number generator state.
                - env_state (State): The environment state.
                - last_timestep (TimeStep): The last timestep in the current trajectory.
            _ (Any): The current metrics info.
        """

        def _env_step(
            learner_state: LearnerState, rollout_idx: int
        ) -> Tuple[LearnerState, PPOTransition]:
            """Step the environment."""
            (
                params,
                opt_states,
                key,
                env_state,
                last_timestep,
                value_norm_params,
                hidden_state,
            ) = learner_state

            # SELECT ACTION
            key, policy_key, perm_key = jax.random.split(key, 3)

            last_obs = last_timestep.observation

            action, log_prob, value, _, hidden_state = actor_action_select_fn(  # type: ignore
                params.actor_params,
                last_obs.agents_view,
                last_obs.action_mask,
                hidden_state,
                policy_key,
            )
            action = jnp.squeeze(action, axis=-1)
            log_prob = jnp.squeeze(log_prob, axis=-1)
            value = jnp.squeeze(value, axis=-1)

            # STEP ENVIRONMENT
            env_state, timestep = jax.vmap(env.step, in_axes=(0, 0))(env_state, action)

            # LOG EPISODE METRICS
            prev_done = jax.tree_util.tree_map(
                lambda x: jnp.repeat(x, config.system.num_agents).reshape(
                    config.arch.num_envs, -1
                ),
                last_timestep.last(),
            )
            # reset hidden state at last timestep
            # need the same number of dims as hidden state
            done = timestep.last()
            done = jnp.expand_dims(done, (1, 2, 3, 4))
            hidden_state = jax.tree_map(
                lambda hs: jnp.where(done, jnp.zeros_like(hs), hs), hidden_state
            )

            # all envs returns to be n_agents lenght
            info = jax.tree_util.tree_map(
                lambda x: jnp.repeat(
                    x[..., jnp.newaxis], config.system.num_agents, axis=-1
                ),
                timestep.extras["episode_metrics"],
            )

            transition = PPOTransition(
                prev_done,
                action,
                value,
                timestep.reward,
                log_prob,
                last_timestep.observation,
                info,
            )
            learner_state = LearnerState(
                params,
                opt_states,
                key,
                env_state,
                timestep,
                value_norm_params,
                hidden_state,
            )
            return learner_state, transition

        prev_hs = jax.tree_map(lambda x: jnp.copy(x), learner_state.hidden_state)
        # STEP ENVIRONMENT FOR ROLLOUT LENGTH
        learner_state, traj_batch = jax.lax.scan(
            _env_step,
            learner_state,
            jnp.arange(config.system.rollout_length),
            config.system.rollout_length,
        )

        # CALCULATE ADVANTAGE
        (
            params,
            opt_states,
            key,
            env_state,
            last_timestep,
            value_norm_params,
            hidden_state,
        ) = learner_state

        key, last_val_key = jax.random.split(key)
        _, _, last_val, _, _ = actor_action_select_fn(  # type: ignore
            params.actor_params,
            last_timestep.observation.agents_view,
            last_timestep.observation.action_mask,
            hidden_state,
            last_val_key,
        )

        last_val = jnp.squeeze(last_val, axis=-1)
        last_done = jax.tree_util.tree_map(
            lambda x: jnp.repeat(x, config.system.num_agents).reshape(
                config.arch.num_envs, -1
            ),
            last_timestep.last(),
        )

        def _calculate_gae(
            traj_batch: PPOTransition,
            last_val: chex.Array,
            last_done: chex.Array,
            value_norm_params: ValueNormParams,
        ) -> Tuple[chex.Array, chex.Array]:
            """Calculate the GAE."""
            normalize_vals = False

            def _get_advantages(
                gae_and_next_value: Tuple, transition: PPOTransition
            ) -> Tuple:
                """Calculate the GAE for a single transition."""
                gae, next_value, next_done = gae_and_next_value
                done, value, reward = (
                    transition.done,
                    transition.value,
                    transition.reward,
                )
                gamma = config.system.gamma
                next_value = denormalise(value_norm_params, next_value, normalize_vals)
                value = denormalise(value_norm_params, value, normalize_vals)

                delta = reward + gamma * next_value * (1 - next_done) - value
                gae = delta + gamma * config.system.gae_lambda * (1 - next_done) * gae
                return (gae, value, done), gae

            _, advantages = jax.lax.scan(
                _get_advantages,
                (jnp.zeros_like(last_val), last_val, last_done),
                traj_batch,
                reverse=True,
                unroll=16,
            )

            batch_value = denormalise(
                value_norm_params, traj_batch.value, normalize_vals
            )
            return advantages, advantages + batch_value

        advantages, targets = _calculate_gae(
            traj_batch, last_val, last_done, value_norm_params
        )

        def _update_epoch(update_state: Tuple, _: Any) -> Tuple:
            """Update the network for a single epoch."""

            def _update_minibatch(train_state: Tuple, batch_info: Tuple) -> Tuple:
                """Update the network for a single minibatch."""

                # UNPACK TRAIN STATE AND BATCH INFO
                params, opt_states, value_norm_params = train_state
                traj_batch, advantages, targets, prev_hs = batch_info

                def _actor_loss_fn(
                    actor_params: FrozenDict,
                    actor_opt_state: OptState,
                    traj_batch: PPOTransition,
                    gae: chex.Array,
                    value_targets: chex.Array,
                    prev_hs,
                ) -> Tuple:
                    """Calculate the actor loss."""
                    # RERUN NETWORK
                    log_prob, value, entropy = actor_apply_fn(  # type: ignore
                        actor_params,
                        traj_batch.obs.agents_view,
                        traj_batch.action,
                        traj_batch.obs.action_mask,
                        prev_hs,
                        traj_batch.done,
                    )
                    log_prob = jnp.squeeze(log_prob, axis=-1)
                    value = jnp.squeeze(value, axis=-1)
                    entropy = jnp.squeeze(entropy, axis=-1)

                    # CALCULATE ACTOR LOSS
                    ratio = jnp.exp(log_prob - traj_batch.log_prob)

                    # Nomalise advantage at minibatch level
                    gae = (gae - gae.mean()) / (gae.std() + 1e-8)

                    loss_actor1 = ratio * gae
                    loss_actor2 = (
                        jnp.clip(
                            ratio,
                            1.0 - config.system.clip_eps,
                            1.0 + config.system.clip_eps,
                        )
                        * gae
                    )
                    loss_actor = -jnp.minimum(loss_actor1, loss_actor2)
                    loss_actor = loss_actor.mean()
                    entropy = entropy.mean()

                    # CALCULATE VALUE LOSS
                    value_pred_clipped = traj_batch.value + (
                        value - traj_batch.value
                    ).clip(-config.system.clip_eps, config.system.clip_eps)

                    value_targets = normalise(
                        value_norm_params,
                        value_targets,
                        False,
                    )

                    if False:
                        # HUBER LOSS
                        value_losses = huber_loss(
                            value, value_targets, config.system.huber_delta
                        )
                        value_losses_clipped = huber_loss(
                            value_pred_clipped, value_targets, config.system.huber_delta
                        )
                        value_loss = jnp.maximum(
                            value_losses, value_losses_clipped
                        ).mean()

                    else:
                        # MSE LOSS
                        value_losses = jnp.square(value - value_targets)
                        value_losses_clipped = jnp.square(
                            value_pred_clipped - value_targets
                        )
                        value_loss = (
                            0.5 * jnp.maximum(value_losses, value_losses_clipped).mean()
                        )

                    total_loss = (
                        loss_actor
                        - config.system.ent_coef * entropy
                        + config.system.vf_coef * value_loss
                    )
                    return total_loss, (loss_actor, entropy, value_loss)

                # CALCULATE ACTOR LOSS
                actor_grad_fn = jax.value_and_grad(_actor_loss_fn, has_aux=True)
                actor_loss_info, actor_grads = actor_grad_fn(
                    params.actor_params,
                    opt_states.actor_opt_state,
                    traj_batch,
                    advantages,
                    targets,
                    prev_hs,
                )

                actor_grads, actor_loss_info = jax.lax.pmean(
                    (actor_grads, actor_loss_info), axis_name="batch"
                )
                # pmean over devices.
                actor_grads, actor_loss_info = jax.lax.pmean(
                    (actor_grads, actor_loss_info), axis_name="device"
                )

                # UPDATE ACTOR PARAMS AND OPTIMISER STATE
                actor_updates, actor_new_opt_state = actor_update_fn(
                    actor_grads, opt_states.actor_opt_state
                )
                actor_new_params = optax.apply_updates(
                    params.actor_params, actor_updates
                )

                # PACK NEW PARAMS AND OPTIMISER STATE
                new_params = Params(actor_new_params, None)
                new_opt_state = OptStates(actor_new_opt_state, None)

                # PACK LOSS INFO
                total_loss = actor_loss_info[0]
                value_loss = actor_loss_info[1][2]
                actor_loss = actor_loss_info[1][0]
                entropy = actor_loss_info[1][1]
                loss_info = {
                    "total_loss": total_loss,
                    "value_loss": value_loss,
                    "actor_loss": actor_loss,
                    "entropy": entropy,
                }

                return (new_params, new_opt_state, value_norm_params), loss_info

            (
                params,
                opt_states,
                traj_batch,
                advantages,
                targets,
                key,
                value_norm_params,
                prev_hs,
            ) = update_state
            key, batch_shuffle_key, agent_shuffle_key = jax.random.split(key, 3)

            # SHUFFLE MINIBATCHES
            batch_size = config.arch.num_envs
            batch_perm = jax.random.permutation(batch_shuffle_key, batch_size)

            # (T, B, A, ...)
            batch = (traj_batch, advantages, targets)
            batch = jax.tree_util.tree_map(
                lambda x: jnp.take(x, batch_perm, axis=1), batch
            )

            agent_perm = jax.random.permutation(
                agent_shuffle_key, config.system.num_agents
            )
            batch = jax.tree_util.tree_map(
                lambda x: jnp.take(x, agent_perm, axis=2), batch
            )

            prev_hs = jax.tree_util.tree_map(
                lambda x: jnp.take(x, batch_perm, axis=0), prev_hs
            )
            # (B, TxA, ...)
            batch = jax.tree_util.tree_map(concat_time_and_agents, batch)
            # (num Mb, B/Mb, TxA, ...)
            minibatches = jax.tree_util.tree_map(
                lambda x: jnp.reshape(
                    x, (config.system.num_minibatches, -1, *x.shape[1:])
                ),
                batch,
            )
            # also minibatch HS
            prev_hs_minibatch = jax.tree_util.tree_map(
                lambda x: jnp.reshape(
                    x, (config.system.num_minibatches, -1, *x.shape[1:])
                ),
                prev_hs,
            )

            # UPDATE MINIBATCHES
            (params, opt_states, value_norm_params), loss_info = jax.lax.scan(
                _update_minibatch,
                (params, opt_states, value_norm_params),
                (*minibatches, prev_hs_minibatch),
            )

            update_state = (
                params,
                opt_states,
                traj_batch,
                advantages,
                targets,
                key,
                value_norm_params,
                prev_hs,
            )
            return update_state, loss_info

        # Before the epochs update the value norm params with all the batch data
        # to get the running mean and variance.
        value_norm_params = update_running_mean_var(
            value_norm_params,
            traj_batch.value,
            False,
        )

        update_state = (
            params,
            opt_states,
            traj_batch,
            advantages,
            targets,
            key,
            value_norm_params,
            prev_hs,
        )

        # UPDATE EPOCHS
        update_state, loss_info = jax.lax.scan(
            _update_epoch, update_state, None, config.system.ppo_epochs
        )

        (
            params,
            opt_states,
            traj_batch,
            advantages,
            targets,
            key,
            value_norm_params,
            _,
        ) = update_state
        learner_state = LearnerState(
            params,
            opt_states,
            key,
            env_state,
            last_timestep,
            value_norm_params,
            hidden_state,
        )

        metric = traj_batch.info

        return learner_state, (metric, loss_info)

    def learner_fn(learner_state: LearnerState) -> ExperimentOutput[LearnerState]:
        """Learner function.

        This function represents the learner, it updates the network parameters
        by iteratively applying the `_update_step` function for a fixed number of
        updates. The `_update_step` function is vectorized over a batch of inputs.

        Args:
            learner_state (NamedTuple):
                - params (Params): The initial model parameters.
                - opt_states (OptStates): The initial optimizer state.
                - key (chex.PRNGKey): The random number generator state.
                - env_state (LogEnvState): The environment state.
                - timesteps (TimeStep): The initial timestep in the initial trajectory.
        """

        batched_update_step = jax.vmap(
            _update_step, in_axes=(0, None), axis_name="batch"
        )

        learner_state, (episode_info, loss_info) = jax.lax.scan(
            batched_update_step, learner_state, None, config.system.num_updates_per_eval
        )
        return ExperimentOutput(
            learner_state=learner_state,
            episode_metrics=episode_info,
            train_metrics=loss_info,
        )

    return learner_fn


def learner_setup(
    env: Environment, keys: chex.Array, config: DictConfig
) -> Tuple[LearnerFn[LearnerState], Actor, LearnerState]:
    """Initialise learner_fn, network, optimiser, environment and states."""
    # Get available TPU cores.
    n_devices = len(jax.devices())

    # Get number of actions and agents.
    num_actions = int(env.action_spec().num_values[0])
    num_agents = env.action_spec().shape[0]
    config.system.num_agents = num_agents
    config.system.num_actions = num_actions

    # PRNG keys.
    key, actor_net_key, _ = keys

    # Initialise observation: Obs for all agents.
    init_x = env.observation_spec().generate_value()
    init_x = jax.tree_util.tree_map(lambda x: x[None, ...], init_x)
    init_dones = jnp.zeros((1, num_agents), dtype=bool)
    init_action = jnp.zeros((1, num_agents), dtype=jnp.int32)

    # Define network and optimiser.
    actor_network = SableNetwork(
        n_block=config.network.actor_network.n_block,
        embed_dim=config.network.actor_network.embed_dim,
        n_head=config.network.actor_network.n_head,
        n_agents=num_agents,
        action_dim=num_actions,
        decay_scaling_factor=config.network.actor_network.decay_scaling_factor,
        action_space_type="discrete",
    )

    actor_lr = make_learning_rate(config.system.actor_lr, config)
    actor_optim = optax.chain(
        optax.clip_by_global_norm(config.system.max_grad_norm),
        optax.adam(actor_lr, eps=1e-5),
    )

    init_hs = get_init_hidden_state(config, config.arch.num_envs)
    init_hs = jax.tree_map(lambda x: x[0, jnp.newaxis], init_hs)

    # Initialise actor params and optimiser state.
    actor_params = actor_network.init(
        actor_net_key,
        init_x.agents_view,
        init_action,
        init_x.action_mask,
        init_hs,
        init_dones,
    )
    actor_opt_state = actor_optim.init(actor_params)

    # Pack params.
    params = Params(actor_params, None)

    # Pack apply and update functions.
    if config.network.chunkwise.use_chunkwise:
        apply_fns = (
            partial(actor_network.apply, method="get_actions"),
            partial(actor_network.apply, method="chunkwise_training"),
        )
    else:
        apply_fns = (
            partial(actor_network.apply, method="get_actions"),
            actor_network.apply,
        )
    update_fns = (actor_optim.update, None)

    # Get batched iterated update and replicate it to pmap it over cores.
    learn = get_learner_fn(env, apply_fns, update_fns, config)
    learn = jax.pmap(learn, axis_name="device")

    # Initialise environment states and timesteps: across devices and batches.
    key, *env_keys = jax.random.split(
        key, n_devices * config.system.update_batch_size * config.arch.num_envs + 1
    )
    env_states, timesteps = jax.vmap(env.reset, in_axes=(0))(
        jnp.stack(env_keys),
    )
    reshape_states = lambda x: x.reshape(
        (n_devices, config.system.update_batch_size, config.arch.num_envs) + x.shape[1:]
    )

    # (devices, update batch size, num_envs, ...)
    env_states = jax.tree_map(reshape_states, env_states)
    timesteps = jax.tree_map(reshape_states, timesteps)

    init_hidden_state = get_init_hidden_state(config, config.arch.num_envs)

    # Load model from checkpoint if specified.
    if config.logger.checkpointing.load_model:
        loaded_checkpoint = Checkpointer(
            model_name=config.logger.system_name,
            **config.logger.checkpointing.load_args,  # Other checkpoint args
        )
        # Restore the learner state from the checkpoint
        restored_params, _ = loaded_checkpoint.restore_params(input_params=params)
        # Update the params
        params = restored_params

    # Define params to be replicated across devices and batches.
    key, step_keys = jax.random.split(key)
    opt_states = OptStates(actor_opt_state, None)
    replicate_learner = (params, opt_states, step_keys)

    # Duplicate learner for update_batch_size.
    broadcast = lambda x: jnp.broadcast_to(
        x, (config.system.update_batch_size,) + x.shape
    )
    replicate_learner = jax.tree_map(broadcast, replicate_learner)
    init_hidden_state = jax.tree_map(broadcast, init_hidden_state)

    value_norm_params = ValueNormParams()
    broadcast_scalar = lambda x: jnp.broadcast_to(x, (config.system.update_batch_size,))
    value_norm_params = jax.tree_map(broadcast_scalar, value_norm_params)

    # Duplicate learner across devices.
    replicate_learner = flax.jax_utils.replicate(
        replicate_learner, devices=jax.devices()
    )
    value_norm_params = flax.jax_utils.replicate(
        value_norm_params, devices=jax.devices()
    )
    init_hidden_state = flax.jax_utils.replicate(
        init_hidden_state, devices=jax.devices()
    )

    # Initialise learner state.
    params, opt_states, step_keys = replicate_learner
    init_learner_state = LearnerState(
        params,
        opt_states,
        step_keys,
        env_states,
        timesteps,
        value_norm_params,
        init_hidden_state,
    )

    return learn, actor_network, init_learner_state


def run_experiment(_config: DictConfig) -> float:
    """Runs experiment."""
    config = copy.deepcopy(_config)

    # Create the enviroments for train and eval.
    env, eval_env = environments.make(config)

    # PRNG keys.
    key, key_e, actor_net_key, critic_net_key = jax.random.split(
        jax.random.PRNGKey(config["system"]["seed"]), num=4
    )

    # Setup learner.
    learn, actor_network, learner_state = learner_setup(
        env, (key, actor_net_key, critic_net_key), config
    )

    # Setup evaluator.
    """evaluator, absolute_metric_evaluator = make_eval_fns(
        eval_env=eval_env,
        network_apply_fn=actor_network.apply,
        config=config,
        use_recurrent_net=True,
        use_mat=True,
        action_space_type="discrete",
    )"""

    # Calculate total timesteps.
    n_devices = len(jax.devices())
    config = check_total_timesteps(config)
    assert (
        config.system.num_updates > config.arch.num_evaluation
    ), "Number of updates per evaluation must be less than total number of updates."

    # Calculate number of updates per evaluation.
    config.system.num_updates_per_eval = (
        config.system.num_updates // config.arch.num_evaluation
    )
    steps_per_rollout = (
        n_devices
        * config.system.num_updates_per_eval
        * config.system.rollout_length
        * config.system.update_batch_size
        * config.arch.num_envs
    )

    # Logger setup
    logger = MavaLogger(config)
    cfg: Dict = OmegaConf.to_container(config, resolve=True)
    cfg["arch"]["devices"] = jax.devices()
    pprint(cfg)

    # Set up checkpointer
    save_checkpoint = config.logger.checkpointing.save_model
    if save_checkpoint:
        checkpointer = Checkpointer(
            metadata=config,  # Save all config as metadata in the checkpoint
            model_name=config.logger.system_name,
            **config.logger.checkpointing.save_args,  # Checkpoint args
        )

    # Run experiment for a total number of evaluations.
    max_episode_return = -jnp.inf
    best_params = None
    for eval_step in range(config.arch.num_evaluation):
        # Train.
        start_time = time.time()

        learner_output = learn(learner_state)
        jax.block_until_ready(learner_output)

        # Log the results of the training.
        elapsed_time = time.time() - start_time
        t = steps_per_rollout * (eval_step + 1)
        episode_metrics, ep_completed = get_final_step_metrics(
            learner_output.episode_metrics
        )
        episode_metrics["steps_per_second"] = steps_per_rollout / elapsed_time

        # Separately log timesteps, actoring metrics and training metrics.
        logger.log({"timestep": t}, t, eval_step, LogEvent.MISC)
        if (
            ep_completed
        ):  # only log episode metrics if an episode was completed in the rollout.
            logger.log(episode_metrics, t, eval_step, LogEvent.ACT)
        logger.log(learner_output.train_metrics, t, eval_step, LogEvent.TRAIN)

        # Prepare for evaluation.
        start_time = time.time()
        trained_params = jax.tree_util.tree_map(
            lambda x: x[:, 0, ...],
            learner_output.learner_state.params.actor_params,  # Select only actor params
        )
        key_e, *eval_keys = jax.random.split(key_e, n_devices + 1)
        eval_keys = jnp.stack(eval_keys)
        eval_keys = eval_keys.reshape(n_devices, -1)

        # Evaluate.
        """evaluator_output = evaluator(trained_params, eval_keys)
        jax.block_until_ready(evaluator_output)

        # Log the results of the evaluation.
        elapsed_time = time.time() - start_time
        episode_return = jnp.mean(evaluator_output.episode_metrics["episode_return"])

        steps_per_eval = int(
            jnp.sum(evaluator_output.episode_metrics["episode_length"])
        )
        evaluator_output.episode_metrics["steps_per_second"] = (
            steps_per_eval / elapsed_time
        )
        logger.log(evaluator_output.episode_metrics, t, eval_step, LogEvent.EVAL)

        if save_checkpoint:
            # Save checkpoint of learner state
            checkpointer.save(
                timestep=steps_per_rollout * (eval_step + 1),
                unreplicated_learner_state=unreplicate_n_dims(
                    learner_output.learner_state
                ),
                episode_return=episode_return,
            )

        if config.arch.absolute_metric and max_episode_return <= episode_return:
            best_params = copy.deepcopy(trained_params)
            max_episode_return = episode_return"""

        # Update runner state to continue training.
        learner_state = learner_output.learner_state

    # Prep metrics to return for sweeping
    """eval_performance = float(
        jnp.mean(evaluator_output.episode_metrics[config.env.eval_metric])
    )

    # Measure absolute metric.
    if config.arch.absolute_metric:
        start_time = time.time()

        key_e, *eval_keys = jax.random.split(key_e, n_devices + 1)
        eval_keys = jnp.stack(eval_keys)
        eval_keys = eval_keys.reshape(n_devices, -1)

        evaluator_output = absolute_metric_evaluator(best_params, eval_keys)
        jax.block_until_ready(evaluator_output)

        elapsed_time = time.time() - start_time
        steps_per_eval = int(
            jnp.sum(evaluator_output.episode_metrics["episode_length"])
        )
        t = int(steps_per_rollout * (eval_step + 1))
        evaluator_output.episode_metrics["steps_per_second"] = (
            steps_per_eval / elapsed_time
        )
        logger.log(evaluator_output.episode_metrics, t, eval_step, LogEvent.ABSOLUTE)"""

    # Stop the logger.
    logger.stop()

    return 0#eval_performance


@hydra.main(
    config_path="../../configs",
    config_name="default_sable_memory.yaml",
    version_base="1.2",
)
def hydra_entry_point(cfg: DictConfig) -> float:
    """Experiment entry point."""
    # Allow dynamic attributes.
    OmegaConf.set_struct(cfg, False)

    # Run experiment.

    eval_performance = run_experiment(cfg)
    jax.block_until_ready(eval_performance)
    print(f"{Fore.CYAN}{Style.BRIGHT}Sable experiment completed{Style.RESET_ALL}")
    return float(eval_performance)


if __name__ == "__main__":
    hydra_entry_point()
