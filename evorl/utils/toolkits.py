import jax
import jax.numpy as jnp

import chex

from typing import Tuple
import os





def jit_method(static_argnums):
    """
    A decorator for `jax.jit` with arguments.

    Args:
        static_argnums: The positional argument indices that are constant across
            different calls to the function.

    Returns:
        A decorator for `jax.jit` with arguments.
    """

    def decorator(f):
        return jax.jit(f, static_argnums=static_argnums)

    return decorator


_vmap_rng_split = jax.vmap(jax.random.split, in_axes=(0, None), out_axes=1)


def vmap_rng_split(key: jax.Array, num: int = 2) -> jax.Array:
    # batched_key [B, 2] -> batched_keys [num, B, 2]
    chex.assert_shape(key, (None, 2))
    return _vmap_rng_split(key, jnp.arange(num))


def tree_zeros_like(nest: chex.ArrayTree, dtype=None) -> chex.ArrayTree:
    return jax.tree_util.tree_map(lambda x: jnp.zeros(x.shape, dtype or x.dtype), nest)


def tree_ones_like(nest: chex.ArrayTree, dtype=None) -> chex.ArrayTree:
    return jax.tree_util.tree_map(lambda x: jnp.ones(x.shape, dtype or x.dtype), nest)


def tree_concat(nest1, nest2, axis=0):
    return jax.tree_util.tree_map(lambda x, y: jnp.concatenate([x, y], axis=axis), nest1, nest2)


@jit_method(static_argnums=(1, 2))
def right_shift(arr: chex.Array, shift: int, pad_val=None) -> chex.Array:
    if pad_val is None:
        padding = jnp.zeros(shift, dtype=arr.dtype)
    else:
        padding = jnp.full(shift, pad_val, dtype=arr.dtype)
    return jnp.concatenate([padding, arr[:-shift]], axis=0)


def compute_gae(dones: jax.Array,  # [T, B]
                rewards: jax.Array,  # [T, B]
                values: jax.Array,  # [T+1, B]
                gae_lambda: float = 1.0,
                discount: float = 0.99) -> Tuple[jax.Array, jax.Array]:
    """
    Calculates the Generalized Advantage Estimation (GAE).

    Args:
        dones: A float32 tensor of shape [T, B] with truncation signal.
        rewards: A float32 tensor of shape [T, B] containing rewards generated by
          following the behaviour policy.
        values: A float32 tensor of shape [T+1, B] with the value function estimates
          wrt. the target policy. values[0] is the bootstrap_value
        gae_lambda: Mix between 1-step (gae_lambda=0) and n-step (gae_lambda=1). 
        discount: TD discount.

    Returns:
        A float32 tensor of shape [T, B]. Can be used as target to
          train a baseline (V(x_t) - vs_t)^2.
        A float32 tensor of shape [T, B] of advantages.
    """
    rewards_shape = rewards.shape
    chex.assert_shape(values, (rewards_shape[0]+1, *rewards_shape[1:]))

    deltas = rewards + discount * (1 - dones) * values[1:] - values[:-1]

    last_gae = jnp.zeros_like(values[0])

    def _compute_gae(gae_t_plus_1, x_t):
        delta_t, factor_t = x_t
        gae_t = delta_t + factor_t * gae_t_plus_1

        return gae_t, gae_t

    _, advantages = jax.lax.scan(
        _compute_gae,
        last_gae,
        (deltas, discount*gae_lambda*(1-dones)),
        reverse=True,
        unroll=16
    )

    lambda_retruns = advantages + values[:-1]

    return jax.lax.stop_gradient(lambda_retruns), jax.lax.stop_gradient(advantages)
