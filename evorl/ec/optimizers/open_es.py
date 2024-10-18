from collections.abc import Callable

import chex
import jax
import jax.numpy as jnp
import jax.tree_util as jtu
import optax

from evorl.types import PyTreeData, pytree_field, Params
from evorl.utils.jax_utils import rng_split_like_tree

from .utils import ExponentialScheduleSpec, weight_sum
from .ec_optimizer import EvoOptimizer, ECState


def compute_ranks(x):
    """
    Returns ranks in [0, len(x))
    Note: This is different from scipy.stats.rankdata, which returns ranks in [1, len(x)].
    """
    assert x.ndim == 1
    ranks = jnp.arange(len(x))[x.argsort(descending=True)]
    return ranks


def compute_centered_ranks(x):
    y = compute_ranks(x)
    y /= x.size - 1
    y -= 0.5
    return y


class OpenESState(PyTreeData):
    mean: chex.ArrayTree
    opt_state: optax.OptState
    noise_std: float
    key: chex.PRNGKey


class OpenES(EvoOptimizer):
    pop_size: int
    lr_schedule: ExponentialScheduleSpec
    noise_std_schedule: ExponentialScheduleSpec
    mirror_sampling: bool = True
    fitness_shaping_fn: Callable[[chex.Array], chex.Array] = pytree_field(
        pytree_node=False, default=compute_centered_ranks
    )
    optimizer: optax.GradientTransformation = pytree_field(
        pytree_node=False, lazy_init=True
    )

    def __post_init__(self):
        assert self.pop_size > 0, "pop_size must be positive"
        if self.mirror_sampling:
            assert self.pop_size % 2 == 0, "pop_size must be even for mirror sampling"

        optimizer = optax.inject_hyperparams(
            optax.adam, static_args=("b1", "b2", "eps", "eps_root")
        )(learning_rate=self.lr_schedule.init)
        self.set_frozen_attr("optimizer", optimizer)

    def init(self, mean: Params, key: chex.PRNGKey) -> ECState:
        return OpenESState(
            mean=mean,
            opt_state=self.optimizer.init(mean),
            noise_std=self.noise_std_schedule.init,
            key=key,
        )

    def tell(
        self, state: ECState, xs: chex.ArrayTree, fitnesses: chex.Array
    ) -> ECState:
        "Update the optimizer state based on the fitnesses of the candidate solutions"

        transformed_fitnesses = self.fitness_shaping_fn(fitnesses)

        opt_state = state.opt_state

        # [pop_size, ...]
        noise = jtu.tree_map(lambda x, m: x - m, xs, state.mean)
        grad = jtu.tree_map(
            # Note: we need additional "-1.0" since we are maximizing the fitness
            lambda d: (
                -weight_sum(d, transformed_fitnesses)
                / (self.pop_size * state.noise_std)
            ),
            noise,
        )
        update, opt_state = self.optimizer.update(grad, state.opt_state)
        mean = optax.apply_updates(state.mean, update)

        opt_state.hyperparams["learning_rate"] = optax.incremental_update(
            self.lr_schedule.final,
            opt_state.hyperparams["learning_rate"],
            1 - self.lr_schedule.decay,
        )

        noise_std = optax.incremental_update(
            self.noise_std_schedule.final,
            state.noise_std,
            1 - self.noise_std_schedule.decay,
        )

        return state.replace(mean=mean, opt_state=opt_state, noise_std=noise_std)

    def ask(self, state: ECState, key: chex.PRNGKey) -> tuple[chex.ArrayTree, ECState]:
        "Generate new candidate solutions"
        key, sample_key = jax.random.split(state.key)

        sample_keys = rng_split_like_tree(sample_key, state.mean)

        if self.mirror_sampling:
            noise = jtu.tree_map(
                lambda x, k: jax.random.normal(k, shape=(self.pop_size // 2, *x.shape)),
                state.mean,
                sample_keys,
            )
            noise = jtu.tree_map(lambda x: jnp.concatenate([x, -x], axis=0), noise)
        else:
            noise = jtu.tree_map(
                lambda x, k: jax.random.normal(k, shape=(self.pop_size, *x.shape)),
                state.mean,
                sample_keys,
            )

        pop = jtu.tree_map(
            lambda m, delta: m + state.noise_std * delta, state.mean, noise
        )
        state = state.replace(key=key)

        return pop, state
