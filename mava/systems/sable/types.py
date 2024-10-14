from typing import Any, Callable, Dict, Generic, Tuple, TypeVar

import chex
from flax.core.frozen_dict import FrozenDict
from optax._src.base import OptState
from tensorflow_probability.substrates.jax.distributions import Distribution
from typing_extensions import NamedTuple, TypeAlias

from jumanji.types import TimeStep

Action: TypeAlias = chex.Array
Value: TypeAlias = chex.Array
Done: TypeAlias = chex.Array
HiddenState: TypeAlias = chex.Array
State: TypeAlias = Any


class Observation(NamedTuple):
    """The observation that the agent sees.
    agents_view: the agent's view of the environment.
    action_mask: boolean array specifying, for each agent, which action is legal.
    step_count: the number of steps elapsed since the beginning of the episode.
    """

    agents_view: chex.Array  # (num_agents, num_obs_features)
    action_mask: chex.Array  # (num_agents, num_actions)
    step_count: chex.Array  # (num_agents, )


class ObservationGlobalState(NamedTuple):
    """The observation seen by agents in centralised systems.
    Extends `Observation` by adding a `global_state` attribute for centralised training.
    global_state: The global state of the environment, often a concatenation of agents' views.
    """

    agents_view: chex.Array  # (num_agents, num_obs_features)
    action_mask: chex.Array  # (num_agents, num_actions)
    global_state: chex.Array  # (num_agents, num_agents * num_obs_features)
    step_count: chex.Array  # (num_agents, )


RNNObservation: TypeAlias = Tuple[Observation, Done]
RNNGlobalObservation: TypeAlias = Tuple[ObservationGlobalState, Done]


################## Needed for MAT/sable ############################
class ValueNormParams(NamedTuple):
    beta: float = 0.99999
    epsilon: float = 1e-5
    running_mean: float = 0.0
    running_mean_sq: float = 0.0
    debiasing_term: float = 0.0


class Params(NamedTuple):
    """Parameters of an actor critic network."""

    actor_params: FrozenDict
    critic_params: FrozenDict


class OptStates(NamedTuple):
    """OptStates of actor critic learner."""

    actor_opt_state: OptState
    critic_opt_state: OptState


class HiddenStates(NamedTuple):
    """Hidden states for an actor critic learner."""

    policy_hidden_state: HiddenState
    critic_hidden_state: HiddenState


class ChunkHiddenStates(NamedTuple):
    """Hidden states for the chunkwise representation."""

    chunkwise_hstate_encoder: HiddenState
    chunkwise_hstate_decoder: HiddenState


class LearnerState(NamedTuple):
    """State of the learner."""

    params: Params
    opt_states: OptStates
    key: chex.PRNGKey
    env_state: State
    timestep: TimeStep


class PPOTransition(NamedTuple):
    """Transition tuple for PPO."""

    done: Done
    action: Action
    value: Value
    reward: chex.Array
    log_prob: chex.Array
    obs: chex.Array
    info: Dict


##############################################
class EvalState(NamedTuple):
    """State of the evaluator."""

    key: chex.PRNGKey
    env_state: State
    timestep: TimeStep
    step_count: chex.Array
    episode_return: chex.Array


class RNNEvalState(NamedTuple):
    """State of the evaluator for recurrent architectures."""

    key: chex.PRNGKey
    env_state: State
    timestep: TimeStep
    dones: chex.Array
    hstate: HiddenState
    step_count: chex.Array
    episode_return: chex.Array


# `MavaState` is the main type passed around in our systems. It is often used as a scan carry.
# Types like: `EvalState` | `LearnerState` (mava/systems/<system_name>/types.py) are `MavaState`s.
MavaState = TypeVar("MavaState")


class ExperimentOutput(NamedTuple, Generic[MavaState]):
    """Experiment output."""

    learner_state: MavaState
    episode_metrics: Dict[str, chex.Array]
    train_metrics: Dict[str, chex.Array]


LearnerFn = Callable[[MavaState], ExperimentOutput[MavaState]]
EvalFn = Callable[[FrozenDict, chex.PRNGKey], ExperimentOutput[MavaState]]

ActorApply = Callable[[FrozenDict, Observation], Distribution]
CriticApply = Callable[[FrozenDict, Observation], Value]
RecActorApply = Callable[
    [FrozenDict, HiddenState, RNNObservation], Tuple[HiddenState, Distribution]
]
RecCriticApply = Callable[
    [FrozenDict, HiddenState, RNNObservation], Tuple[HiddenState, Value]
]
