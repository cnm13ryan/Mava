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


from typing import Tuple

import chex
import jax
import jax.numpy as jnp
from flax import linen as nn
from omegaconf import DictConfig


class PositionalEncoding(nn.Module):
    """Positional Encoding for Sable. Encodes position information into sequences"""

    memory_config: DictConfig
    d_model: int

    def setup(self) -> None:
        # Set maximum sequence length for positional encoding
        self.max_size = 10_000
        # Precompute the scaling factor for even indices (used in sine and cosine functions)
        self.div_term = jnp.exp(
            jnp.arange(0, self.d_model, 2) * (-jnp.log(10000.0) / self.d_model)
        )[jnp.newaxis]
        # Add a flag to enable positional encoding based on the network type
        self.do_pos_enc = (
            self.memory_config.type == "rec_sable"
        ) and self.memory_config.timestep_positional_encoding

    def __call__(
        self, key: chex.Array, query: chex.Array, value: chex.Array, position: chex.Array
    ) -> Tuple[chex.Array, chex.Array, chex.Array]:
        """Computes positional encoding for a given sequence of positions."""
        # Check if positional encoding is enabled
        if self.do_pos_enc:
            pe = jax.vmap(self._get_pos_encoding)(position)

            # Add positional encoding to the input tensors
            key += pe
            query += pe
            value += pe

        return key, query, value

    def _get_pos_encoding(self, position: chex.Array) -> chex.Array:
        """Computes positional encoding for a given the index of the token."""
        seq_len = position.shape[0]

        # Calculate positional encoding using sine for even indices and cosine for odd indices.
        x = position[:, jnp.newaxis] * self.div_term
        pe = jnp.zeros((seq_len, self.d_model))
        pe = pe.at[:, 0::2].set(jnp.sin(x))
        pe = pe.at[:, 1::2].set(jnp.cos(x))

        return pe
