# Copyright 2022 InstaDeep Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from functools import partial
from typing import Any, Optional, Tuple

import chex
import jax.numpy as jnp
from flax import linen as nn
from flax.linen.initializers import orthogonal
from jax import tree
from omegaconf import DictConfig

from mava.networks.retention import MultiScaleRetention
from mava.networks.torsos import SwiGLU
from mava.networks.utils.sable.discrete_trainer_executor import *  # noqa
from mava.systems.sable.types import HiddenStates, SableNetworkConfig
from mava.types import Observation


class EncodeBlock(nn.Module):
    """Sable encoder block."""

    net_config: SableNetworkConfig
    memory_config: DictConfig
    n_agents: int

    def setup(self) -> None:
        self.ln1 = nn.RMSNorm()
        self.ln2 = nn.RMSNorm()

        self.retn = MultiScaleRetention(
            embed_dim=self.net_config.embed_dim,
            n_head=self.net_config.n_head,
            n_agents=self.n_agents,
            full_self_retention=True,  # Full retention for the encoder
            memory_config=self.memory_config,
            decay_scaling_factor=self.memory_config.decay_scaling_factor,
        )

        self.ffn = SwiGLU(self.net_config.embed_dim, self.net_config.embed_dim)

    def __call__(
        self, x: chex.Array, hstate: chex.Array, dones: chex.Array, step_count: chex.Array
    ) -> chex.Array:
        """Applies Chunkwise MultiScaleRetention."""
        ret, updated_hstate = self.retn(
            key=x, query=x, value=x, hstate=hstate, dones=dones, step_count=step_count
        )
        x = self.ln1(x + ret)
        output = self.ln2(x + self.ffn(x))
        return output, updated_hstate

    def recurrent(self, x: chex.Array, hstate: chex.Array, step_count: chex.Array) -> chex.Array:
        """Applies Recurrent MultiScaleRetention."""
        ret, updated_hstate = self.retn.recurrent(
            key_n=x, query_n=x, value_n=x, hstate=hstate, step_count=step_count
        )
        x = self.ln1(x + ret)
        output = self.ln2(x + self.ffn(x))
        return output, updated_hstate


class Encoder(nn.Module):
    """Multi-block encoder consisting of multiple `EncoderBlock` modules."""

    net_config: SableNetworkConfig
    memory_config: DictConfig
    n_agents: int

    def setup(self) -> None:
        self.ln = nn.RMSNorm()

        self.obs_encoder = nn.Sequential(
            [
                nn.RMSNorm(),
                nn.Dense(
                    self.net_config.embed_dim, kernel_init=orthogonal(jnp.sqrt(2)), use_bias=False
                ),
                nn.gelu,
            ],
        )
        self.head = nn.Sequential(
            [
                nn.Dense(self.net_config.embed_dim, kernel_init=orthogonal(jnp.sqrt(2))),
                nn.gelu,
                nn.RMSNorm(),
                nn.Dense(1, kernel_init=orthogonal(0.01)),
            ],
        )

        self.blocks = [
            EncodeBlock(
                self.net_config,
                self.memory_config,
                self.n_agents,
                name=f"encoder_block_{block_id}",
            )
            for block_id in range(self.net_config.n_block)
        ]

    def __call__(
        self, obs: chex.Array, hstate: chex.Array, dones: chex.Array, step_count: chex.Array
    ) -> Tuple[chex.Array, chex.Array, chex.Array]:
        """Apply chunkwise encoding."""
        updated_hstate = jnp.zeros_like(hstate)
        obs_rep = self.obs_encoder(obs)

        # Apply the encoder blocks
        for i, block in enumerate(self.blocks):
            hs = hstate[:, :, i]  # Get the hidden state for the current block
            # Apply the chunkwise encoder block
            obs_rep, hs_new = block(self.ln(obs_rep), hs, dones, step_count)
            updated_hstate = updated_hstate.at[:, :, i].set(hs_new)

        value = self.head(obs_rep)

        return value, obs_rep, updated_hstate

    def recurrent(
        self, obs: chex.Array, hstate: chex.Array, step_count: chex.Array
    ) -> Tuple[chex.Array, chex.Array, chex.Array]:
        """Apply recurrent encoding."""
        updated_hstate = jnp.zeros_like(hstate)
        obs_rep = self.obs_encoder(obs)

        # Apply the encoder blocks
        for i, block in enumerate(self.blocks):
            hs = hstate[:, :, i]  # Get the hidden state for the current block
            # Apply the recurrent encoder block
            obs_rep, hs_new = block.recurrent(self.ln(obs_rep), hs, step_count)
            updated_hstate = updated_hstate.at[:, :, i].set(hs_new)

        # Compute the value function
        value = self.head(obs_rep)

        return value, obs_rep, updated_hstate


class DecodeBlock(nn.Module):
    """Sable decoder block."""

    net_config: SableNetworkConfig
    memory_config: DictConfig
    n_agents: int

    def setup(self) -> None:
        self.ln1, self.ln2, self.ln3 = nn.RMSNorm(), nn.RMSNorm(), nn.RMSNorm()

        self.retn1 = MultiScaleRetention(
            embed_dim=self.net_config.embed_dim,
            n_head=self.net_config.n_head,
            n_agents=self.n_agents,
            full_self_retention=False,  # Masked retention for the decoder
            memory_config=self.memory_config,
            decay_scaling_factor=self.memory_config.decay_scaling_factor,
        )
        self.retn2 = MultiScaleRetention(
            embed_dim=self.net_config.embed_dim,
            n_head=self.net_config.n_head,
            n_agents=self.n_agents,
            full_self_retention=False,  # Masked retention for the decoder
            memory_config=self.memory_config,
            decay_scaling_factor=self.memory_config.decay_scaling_factor,
        )

        self.ffn = SwiGLU(self.net_config.embed_dim, self.net_config.embed_dim)

    def __call__(
        self,
        x: chex.Array,
        obs_rep: chex.Array,
        hstates: Tuple[chex.Array, chex.Array],
        dones: chex.Array,
        step_count: chex.Array,
    ) -> Tuple[chex.Array, Tuple[chex.Array, chex.Array]]:
        """Applies Chunkwise MultiScaleRetention."""
        hs1, hs2 = hstates

        # Apply the self-retention over actions
        ret, hs1_new = self.retn1(
            key=x, query=x, value=x, hstate=hs1, dones=dones, step_count=step_count
        )
        ret = self.ln1(x + ret)

        # Apply the cross-retention over obs x action
        ret2, hs2_new = self.retn2(
            key=ret,
            query=obs_rep,
            value=ret,
            hstate=hs2,
            dones=dones,
            step_count=step_count,
        )
        y = self.ln2(obs_rep + ret2)
        output = self.ln3(y + self.ffn(y))

        return output, (hs1_new, hs2_new)

    def recurrent(
        self,
        x: chex.Array,
        obs_rep: chex.Array,
        hstates: Tuple[chex.Array, chex.Array],
        step_count: chex.Array,
    ) -> Tuple[chex.Array, Tuple[chex.Array, chex.Array]]:
        """Applies Recurrent MultiScaleRetention."""
        hs1, hs2 = hstates

        # Apply the self-retention over actions
        ret, hs1_new = self.retn1.recurrent(
            key_n=x, query_n=x, value_n=x, hstate=hs1, step_count=step_count
        )
        ret = self.ln1(x + ret)

        # Apply the cross-retention over obs x action
        ret2, hs2_new = self.retn2.recurrent(
            key_n=ret, query_n=obs_rep, value_n=ret, hstate=hs2, step_count=step_count
        )
        y = self.ln2(obs_rep + ret2)
        output = self.ln3(y + self.ffn(y))

        return output, (hs1_new, hs2_new)


class Decoder(nn.Module):
    """Multi-block decoder consisting of multiple `DecoderBlock` modules."""

    net_config: SableNetworkConfig
    memory_config: DictConfig
    n_agents: int
    action_dim: int
    action_space_type: str = "discrete"

    def setup(self) -> None:
        self.ln = nn.RMSNorm()

        if self.action_space_type == "discrete":
            self.action_encoder = nn.Sequential(
                [
                    nn.Dense(
                        self.net_config.embed_dim,
                        use_bias=False,
                        kernel_init=orthogonal(jnp.sqrt(2)),
                    ),
                    nn.gelu,
                ],
            )
            self.log_std = None
        else:
            self.action_encoder = nn.Sequential(
                [nn.Dense(self.net_config.embed_dim, kernel_init=orthogonal(jnp.sqrt(2))), nn.gelu],
            )
            self.log_std = self.param("log_std", nn.initializers.zeros, (self.action_dim,))

        self.head = nn.Sequential(
            [
                nn.Dense(self.net_config.embed_dim, kernel_init=orthogonal(jnp.sqrt(2))),
                nn.gelu,
                nn.RMSNorm(),
                nn.Dense(self.action_dim, kernel_init=orthogonal(0.01)),
            ],
        )

        self.blocks = [
            DecodeBlock(
                self.net_config,
                self.memory_config,
                self.n_agents,
                name=f"decoder_block_{block_id}",
            )
            for block_id in range(self.net_config.n_block)
        ]

    def __call__(
        self,
        action: chex.Array,
        obs_rep: chex.Array,
        hstates: Tuple[chex.Array, chex.Array],
        dones: chex.Array,
        step_count: chex.Array,
    ) -> Tuple[chex.Array, Tuple[chex.Array, chex.Array]]:
        """Apply chunkwise decoding."""
        updated_hstates = tree.map(jnp.zeros_like, hstates)
        action_embeddings = self.action_encoder(action)
        x = self.ln(action_embeddings)

        # Apply the decoder blocks
        for i, block in enumerate(self.blocks):
            hs = tree.map(lambda x, j=i: x[:, :, j], hstates)
            x, hs_new = block(x=x, obs_rep=obs_rep, hstates=hs, dones=dones, step_count=step_count)
            updated_hstates = tree.map(
                lambda x, y, j=i: x.at[:, :, j].set(y), updated_hstates, hs_new
            )

        logit = self.head(x)

        return logit, updated_hstates

    def recurrent(
        self,
        action: chex.Array,
        obs_rep: chex.Array,
        hstates: Tuple[chex.Array, chex.Array],
        step_count: chex.Array,
    ) -> Tuple[chex.Array, Tuple[chex.Array, chex.Array]]:
        """Apply recurrent decoding."""
        updated_hstates = tree.map(jnp.zeros_like, hstates)
        action_embeddings = self.action_encoder(action)
        x = self.ln(action_embeddings)

        # Apply the decoder blocks
        for i, block in enumerate(self.blocks):
            hs = tree.map(lambda x, i=i: x[:, :, i], hstates)
            x, hs_new = block.recurrent(x=x, obs_rep=obs_rep, hstates=hs, step_count=step_count)
            updated_hstates = tree.map(
                lambda x, y, j=i: x.at[:, :, j].set(y), updated_hstates, hs_new
            )

        logit = self.head(x)

        return logit, updated_hstates


class SableNetwork(nn.Module):
    """Sable network module."""

    n_agents: int
    action_dim: int
    net_config: SableNetworkConfig
    memory_config: DictConfig
    action_space_type: str = "discrete"

    def setup(self) -> None:
        assert self.action_space_type in [
            "discrete",
        ], "Invalid action space type"

        # Set the chunksize differently in ff and recurrent sable
        self.n_agents_per_chunk = self.n_agents
        if self.memory_config.use_chunkwise:
            if self.memory_config.type == "ff_sable":
                self.memory_config.chunk_size = self.memory_config.agents_chunk_size
                err = "Number of agents should be divisible by chunk size"
                assert self.n_agents % self.memory_config.chunk_size == 0, err
                self.n_agents_per_chunk = self.memory_config.chunk_size
            else:
                self.memory_config.chunk_size = (
                    self.memory_config.timestep_chunk_size * self.n_agents
                )

        # Create dummy decay scale factor for FF Sable
        if self.memory_config.type == "ff_sable":
            self.memory_config.decay_scaling_factor = 1.0
        assert (
            self.memory_config.decay_scaling_factor >= 0
            and self.memory_config.decay_scaling_factor <= 1
        ), "Decay scaling factor should be between 0 and 1"

        # Decay kappa for each head
        self.decay_kappas = 1 - jnp.exp(
            jnp.linspace(jnp.log(1 / 32), jnp.log(1 / 512), self.net_config.n_head)
        )
        self.decay_kappas = self.decay_kappas * self.memory_config.decay_scaling_factor
        self.decay_kappas = self.decay_kappas[None, :, None, None, None]

        self.encoder = Encoder(
            self.net_config,
            self.memory_config,
            self.n_agents_per_chunk,
        )
        self.decoder = Decoder(
            self.net_config,
            self.memory_config,
            self.n_agents_per_chunk,
            self.action_dim,
            self.action_space_type,
        )

        # Set the executor and trainer functions
        (
            self.train_encoder_fn,
            self.train_decoder_fn,
            self.execute_encoder_fn,
            self.autoregressive_act,
        ) = self.setup_executor_trainer_fn()

    def __call__(
        self,
        obs_carry: Observation,
        action: chex.Array,
        hstates: HiddenStates,
        dones: chex.Array,
        rng_key: Optional[chex.PRNGKey] = None,
    ) -> Tuple[chex.Array, chex.Array, chex.Array]:
        """Training phase."""
        obs, legal_actions, step_count = (
            obs_carry.agents_view,
            obs_carry.action_mask,
            obs_carry.step_count,
        )
        v_loc, obs_rep, _ = self.train_encoder_fn(
            encoder=self.encoder, obs=obs, hstate=hstates[0], dones=dones, step_count=step_count
        )

        action_log, entropy = self.train_decoder_fn(
            decoder=self.decoder,
            obs_rep=obs_rep,
            action=action,
            legal_actions=legal_actions,
            hstates=hstates[1],
            dones=dones,
            step_count=step_count,
            rng_key=rng_key,
        )

        return v_loc, action_log, entropy

    def get_actions(
        self,
        obs_carry: Observation,
        hstates: HiddenStates,
        key: chex.PRNGKey,
    ) -> Tuple[chex.Array, chex.Array, chex.Array, HiddenStates]:
        """Inference phase."""
        obs, legal_actions, step_count = (
            obs_carry.agents_view,
            obs_carry.action_mask,
            obs_carry.step_count,
        )

        # Decay the hidden states: each timestep we decay the hidden states once
        decayed_hstates = tree.map(lambda x: x * self.decay_kappas, hstates)

        v_loc, obs_rep, updated_enc_hs = self.execute_encoder_fn(
            encoder=self.encoder,
            obs=obs,
            decayed_hstate=decayed_hstates[0],
            step_count=step_count,
        )

        output_actions, output_actions_log, updated_dec_hs = self.autoregressive_act(
            decoder=self.decoder,
            obs_rep=obs_rep,
            legal_actions=legal_actions,
            hstates=decayed_hstates[1],
            step_count=step_count,
            key=key,
        )

        # Pack the hidden states
        updated_hs = HiddenStates(encoder=updated_enc_hs, decoder=updated_dec_hs)
        return output_actions, output_actions_log, v_loc, updated_hs

    def init_net(
        self,
        obs_carry: Observation,
        hstates: HiddenStates,
        key: chex.PRNGKey,
    ) -> Any:
        """Initializating the network."""

        return init_sable(  # noqa
            encoder=self.encoder,
            decoder=self.decoder,
            obs_carry=obs_carry,
            hstates=hstates,
            key=key,
        )

    def setup_executor_trainer_fn(self) -> Tuple:
        """Setup the executor and trainer functions."""

        # Set the executing encoder function based on the chunkwise setting.
        if self.memory_config.use_chunkwise:
            # Define the trainer encoder in chunkwise setting.
            train_enc_fn = partial(
                train_encoder_chunkwise,  # noqa
                chunk_size=self.memory_config.chunk_size,
            )
            # Define the trainer decoder in chunkwise setting.
            act_fn = partial(act_chunkwise, chunk_size=self.memory_config.chunk_size)  # noqa
            train_dec_fn = partial(train_decoder_fn, act_fn=act_fn, n_agents=self.n_agents)  # noqa
            # Define the executor encoder in chunkwise setting.
            if self.memory_config.type == "ff_sable":
                execute_enc_fn = partial(
                    execute_encoder_chunkwise,  # noqa
                    chunk_size=self.memory_config.chunk_size,
                )
            else:
                execute_enc_fn = partial(execute_encoder_parallel)  # noqa
        else:
            # Define the trainer encode when dealing with full sequence setting.
            train_enc_fn = partial(train_encoder_parallel)  # noqa
            # Define the trainer decoder when dealing with full sequence setting.
            train_dec_fn = partial(train_decoder_fn, act_fn=act_parallel, n_agents=self.n_agents)  # noqa
            # Define the executor encoder when dealing with full sequence setting.
            execute_enc_fn = partial(execute_encoder_parallel)  # noqa

        return train_enc_fn, train_dec_fn, execute_enc_fn, autoregressive_act  # noqa
