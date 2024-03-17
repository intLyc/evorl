import copy
import jax
import jax.numpy as jnp
from jax import vmap
from jax.tree_util import tree_leaves, tree_map
from flax import struct
import chex

import flashbax.buffers.trajectory_buffer

from typing import (
    Any, Mapping, Union, Tuple, Dict, Optional, Sequence,
    Protocol, Callable, Iterable
)

from evorl.utils.toolkits import right_shift


BatchedPRNGKey = jax.Array # [B, 2]
Metrics = Mapping[str, chex.ArrayTree]
Observation = chex.Array
Action = chex.Array
PolicyExtraInfo = Mapping[str, Any]
ExtraInfo = Mapping[str, Any]
RewardDict = Mapping[str, chex.ArrayTree]

LossDict = Mapping[str, chex.Array]

# TODO: test it
EnvState = Mapping[str, chex.ArrayTree]

Params = chex.ArrayTree
ObsPreprocessorParams = Mapping[str, Any]
ActionPostprocessorParams = Mapping[str, Any]


ReplayBufferState = Union[flashbax.buffers.trajectory_buffer.TrajectoryBufferState, chex.ArrayTree]


class ObsPreprocessorFn(Protocol):
    def __call__(self, obs: chex.Array, *args: Any, **kwds: Any) -> chex.Array:
        return obs

@struct.dataclass
class Base:
    """Base functionality extending all brax types.

    These methods allow for brax types to be operated like arrays/matrices.
    """

    def __add__(self, o: Any) -> Any:
        return tree_map(lambda x, y: x + y, self, o)

    def __sub__(self, o: Any) -> Any:
        return tree_map(lambda x, y: x - y, self, o)

    def __mul__(self, o: Any) -> Any:
        return tree_map(lambda x: x * o, self)

    def __neg__(self) -> Any:
        return tree_map(lambda x: -x, self)

    def __truediv__(self, o: Any) -> Any:
        return tree_map(lambda x: x / o, self)

    def reshape(self, shape: Sequence[int]) -> Any:
        return tree_map(lambda x: x.reshape(shape), self)

    def select(self, o: Any, cond: jax.Array) -> Any:
        return tree_map(lambda x, y: (x.T * cond + y.T * (1 - cond)).T, self, o)

    def slice(self, beg: int, end: int) -> Any:
        return tree_map(lambda x: x[beg:end], self)

    def take(self, i, axis=0) -> Any:
        return tree_map(lambda x: jnp.take(x, i, axis=axis, mode='wrap'), self)

    def concatenate(self, *others: Any, axis: int = 0) -> Any:
        return tree_map(lambda *x: jnp.concatenate(x, axis=axis), self, *others)

    def index_set(
        self, idx: Union[jax.Array, Sequence[jax.Array]], o: Any
    ) -> Any:
        return tree_map(lambda x, y: x.at[idx].set(y), self, o)

    def index_sum(
        self, idx: Union[jax.Array, Sequence[jax.Array]], o: Any
    ) -> Any:
        return tree_map(lambda x, y: x.at[idx].add(y), self, o)

    # def vmap(self, in_axes=0, out_axes=0):
    #   """Returns an object that vmaps each follow-on instance method call."""

    #   # TODO: i think this is kinda handy, but maybe too clever?

    #   outer_self = self

    #   class VmapField:
    #     """Returns instance method calls as vmapped."""

    #     def __init__(self, in_axes, out_axes):
    #       self.in_axes = [in_axes]
    #       self.out_axes = [out_axes]

    #     def vmap(self, in_axes=0, out_axes=0):
    #       self.in_axes.append(in_axes)
    #       self.out_axes.append(out_axes)
    #       return self

    #     def __getattr__(self, attr):
    #       fun = getattr(outer_self.__class__, attr)
    #       # load the stack from the bottom up
    #       vmap_order = reversed(list(zip(self.in_axes, self.out_axes)))
    #       for in_axes, out_axes in vmap_order:
    #         fun = vmap(fun, in_axes=in_axes, out_axes=out_axes)
    #       fun = functools.partial(fun, outer_self)
    #       return fun

    #   return VmapField(in_axes, out_axes)

    def tree_replace(
        self, params: Dict[str, Optional[jax.typing.ArrayLike]]
    ) -> 'Base':
        """Creates a new object with parameters set.

        Args:
          params: a dictionary of key value pairs to replace

        Returns:
          data clas with new values

        Example:
          If a system has 3 links, the following code replaces the mass
          of each link in the System:
          >>> sys = sys.tree_replace(
          >>>     {'link.inertia.mass', jnp.array([1.0, 1.2, 1.3])})
        """
        new = self
        for k, v in params.items():
            new = _tree_replace(new, k.split('.'), v)
        return new

    @property
    def T(self):  # pylint:disable=invalid-name
        return tree_map(lambda x: x.T, self)


def _tree_replace(
    base: Base,
    attr: Sequence[str],
    val: Optional[jax.typing.ArrayLike],
) -> Base:
    """Sets attributes in a struct.dataclass with values."""
    if not attr:
        return base

    # special case for List attribute
    if len(attr) > 1 and isinstance(getattr(base, attr[0]), list):
        lst = copy.deepcopy(getattr(base, attr[0]))

        for i, g in enumerate(lst):
            if not hasattr(g, attr[1]):
                continue
            v = val if not hasattr(val, '__iter__') else val[i]
            lst[i] = _tree_replace(g, attr[1:], v)

        return base.replace(**{attr[0]: lst})

    if len(attr) == 1:
        return base.replace(**{attr[0]: val})

    return base.replace(
        **{attr[0]: _tree_replace(getattr(base, attr[0]), attr[1:], val)}
    )


class EnvLike(Protocol):
    """
        Use Brax.Env style API
        For gymnax-style envs (eg: gymnax, minmax), use gymnax.wrappers.brax.GymnaxToBraxWrapper
    """

    def reset(self, rng: chex.PRNGKey, *args, **kwargs) -> EnvState:
        """Resets the environment to an initial state."""
        pass

    def step(self, state: EnvState, action: Action, *args, **kwargs) -> EnvState:
        """Run one timestep of the environment's dynamics."""
        pass


@struct.dataclass
class SampleBatch(Base):
    """
      Batched transitions w/ additional first axis as batch_axis.
      Could also be used as a trajectory.
    """
    # TODO: skip None in tree_map (should be work in native jax)
    obs: Union[chex.ArrayTree, None] = None
    action: Union[chex.ArrayTree, None] = None
    reward: Union[chex.ArrayTree, RewardDict, None] = None
    next_obs: Union[chex.Array, None] = None
    done: Union[chex.Array, None] = None
    extras: Union[ExtraInfo, None] = None

    def append(self, values: chex.ArrayTree, axis: int = 0) -> Any:
        return tree_map(lambda x, v: jnp.append(x, v, axis=axis), self, values)

    def __len__(self):
        return tree_leaves(self.obs)[0].shape[0]
    
    @staticmethod
    def create_dummy_sample_batch(env, env_state):
        obs = env.observation(env_state)
        action = env.action_space.sample()
        next_obs = env.observation(env_state)
        reward = jnp.zeros((1,))
        done = jnp.zeros((1,))
        return SampleBatch(obs=obs, action=action, reward=reward, next_obs=next_obs, done=done)


@struct.dataclass
class Episode:
    trajectory: SampleBatch
    last_obs: chex.ArrayTree

    @property
    def valid_mask(self) -> chex.Array:
        return 1-right_shift(self.trajectory.done, 1)


@struct.dataclass
class RolloutMetric:
    timesteps: int = 0





