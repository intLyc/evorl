# Copyright 2024 The Brax Authors.
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

"""Brax training gradient utility functions."""

from typing import Callable, Optional

import jax
import chex
import optax


def loss_and_pgrad(loss_fn: Callable[..., float],
                   pmap_axis_name: Optional[str],
                   has_aux: bool = False):
    g = jax.value_and_grad(loss_fn, has_aux=has_aux)

    def h(*args, **kwargs):
        value, grad = g(*args, **kwargs)
        return value, jax.lax.pmean(grad, axis_name=pmap_axis_name)

    return g if pmap_axis_name is None else h


def gradient_update(loss_fn: Callable[..., float],
                    optimizer: optax.GradientTransformation,
                    pmap_axis_name: Optional[str],
                    has_aux: bool = False):
    """Wrapper of the loss function that apply gradient updates.

    Args:
      loss_fn: The loss function. (params, ...) -> loss
      optimizer: The optimizer to apply gradients.
      pmap_axis_name: If relevant, the name of the pmap axis to synchronize
        gradients.
      has_aux: Whether the loss_fn has auxiliary data.

    Returns:
      A function that takes the same argument as the loss function plus the
      optimizer state. The output of this function is the loss, the new parameter,
      and the new optimizer state.
    """
    loss_and_pgrad_fn = loss_and_pgrad(
        loss_fn, pmap_axis_name=pmap_axis_name, has_aux=has_aux)

    def f(optimizer_state, params, *args, **kwargs):
        value, grads = loss_and_pgrad_fn(*args, **kwargs)
        params_update, optimizer_state = optimizer.update(
            grads, optimizer_state)
        params = optax.apply_updates(args[0], params_update)
        return value, params, optimizer_state

    return f


def agent_gradient_update(loss_fn: Callable[..., float],
                          optimizer: optax.GradientTransformation,
                          pmap_axis_name: Optional[str],
                          has_aux: bool = False):
    def _loss_fn(params, agent_state, sample_batch, key):
        return loss_fn(agent_state.replace(params=params),
                       sample_batch, key)

    loss_and_pgrad_fn = loss_and_pgrad(
        _loss_fn, pmap_axis_name=pmap_axis_name, has_aux=has_aux)

    def f(opt_state, agent_state, *args, **kwargs):
        value, grads = loss_and_pgrad_fn(
            agent_state.params, agent_state, *args, **kwargs)
        params_update, opt_state = optimizer.update(
            grads, opt_state)
        params = optax.apply_updates(agent_state.params, params_update)

        agent_state.replace(params=params)
        return value, agent_state, opt_state

    return f
