from optax.schedules import InjectStatefulHyperparamsState
import jax
import jax.numpy as jnp
import jax.tree_util as jtu

from jax.sharding import Mesh
from jax.sharding import PartitionSpec as P
from jax.sharding import NamedSharding
from jax.experimental import mesh_utils

from flax import struct
import math

import hydra
from omegaconf import DictConfig, open_dict, read_write

from functools import partial

from evorl.sample_batch import SampleBatch
from evorl.networks import make_policy_network, make_value_network
from evorl.utils import running_statistics
from evorl.distribution import get_categorical_dist, get_tanh_norm_dist
from evorl.utils.jax_utils import tree_stop_gradient
from evorl.utils.toolkits import (
    compute_gae, flatten_rollout_trajectory,
    average_episode_discount_return
)
from evorl.workflows import OnPolicyRLWorkflow, RLWorkflow
from evorl.agents import AgentState
from evorl.distributed import split_key_to_devices, POP_AXIS_NAME
from evorl.envs import create_env, Env, EnvState
from evorl.evaluator import Evaluator
from evorl.metrics import MetricBase
from ..agent import Agent, AgentState

from evox import State
# from evorl.types import State


import orbax.checkpoint as ocp
import chex
import optax
from evorl.types import (
    PyTreeNode, PyTreeDict
)
from evorl.metrics import TrainMetric, WorkflowMetric
from typing import Tuple, Sequence, Optional, Any
import logging
import flax.linen as nn
from flax import struct
import copy
from omegaconf import OmegaConf

from functools import partial

logger = logging.getLogger(__name__)

class TrainMetric(MetricBase):
    pop_episode_returns: chex.Array
    pop_episode_lengths: chex.Array


class EvalMetric(MetricBase):
    pop_episode_returns: chex.Array
    pop_episode_lengths: chex.Array


class HyperParams(PyTreeNode):
    lr: chex.Array


class PBTWorkflow(RLWorkflow):
    def __init__(self,
                 workflow: RLWorkflow,
                 config: DictConfig):
        super(PBTWorkflow, self).__init__(config)

        self.workflow = workflow

        self.sharding = None

    @classmethod
    def name(cls):
        return "PBT"

    @staticmethod
    def _rescale_config(config, devices) -> None:
        num_devices = len(devices)

        if  config.pop_size % num_devices != 0:
            logger.warning(
                f"pop_size({config.pop_size}) cannot be divided by num_devices({num_devices}), "
                f"rescale pop_size to {config.pop_size // num_devices * num_devices}"
            )

        config.pop_size = (config.pop_size // num_devices) * num_devices

    @classmethod
    def build_from_config(cls, config: DictConfig, devices: Optional[Sequence[jax.Device]] = None, enable_jit: bool = True):
        config = copy.deepcopy(config)  # avoid in-place modification
        if devices is None:
            devices = jax.local_devices()

        OmegaConf.set_readonly(config, False)
        cls._rescale_config(config, devices)

        if enable_jit:
            cls.enable_jit()

        OmegaConf.set_readonly(config, True)

        workflow = cls._build_from_config(config)

        workflow.pmap_axis_name = None # we use shmap instead.
        workflow.devices = devices

        mesh = Mesh(devices, axis_names=(POP_AXIS_NAME,))
        workflow.sharding = NamedSharding(mesh, P(POP_AXIS_NAME))

        return workflow

    @classmethod
    def _build_from_config(cls, config: DictConfig):
        target_workflow_config = config.target_workflow
        target_workflow_config = copy.deepcopy(target_workflow_config)
        target_workflow_cls = hydra.utils.get_class(
            target_workflow_config.workflow_cls)

        devices = jax.local_devices()

        # OmegaConf.set_struct(target_workflow_config, True)

        with read_write(target_workflow_config):
            with open_dict(target_workflow_config):
                target_workflow_config.env = copy.deepcopy(config.env)
                # disable target workflow ckpt
                target_workflow_config.checkpoint = OmegaConf.create(
                    dict(enable=False))

        OmegaConf.set_readonly(target_workflow_config, True)

        target_workflow = target_workflow_cls.build_from_config(
            target_workflow_config, enable_jit=True
        )

        target_workflow.devices = devices

        return cls(target_workflow, config)

    def setup(self, key: chex.PRNGKey):
        pop_size = self.config.pop_size

        key, workflow_key, pop_key = jax.random.split(key, num=3)
        self.workflow.optimizer = optax.inject_hyperparams(
            optax.adam, static_args=('b1', 'b2', 'eps', 'eps_root')
        )(learning_rate=self.config.search_space.lr.low)

        # save pop&metric on GPU0
        pop_local = PyTreeDict(
            lr=jax.random.uniform(
                pop_key, (pop_size,),
                minval=self.config.search_space.lr.low,
                maxval=self.config.search_space.lr.high
            )
        )
        workflow_metrics = self._setup_workflow_metrics()


        num_devices = len(self.devices)

        workflow_keys = jax.random.split(workflow_key, pop_size)
        workflow_keys = jax.device_put(workflow_keys, self.sharding)

        pop_workflow_state = jax.vmap(self.workflow.setup)(
            workflow_keys
        )

        pop = jtu.tree_map(lambda x: jax.device_put(x, self.sharding), pop_local)
        pop_workflow_state = jax.vmap(apply_hyperparams_to_workflow_state)(
            pop, pop_workflow_state
        )

        return State(
            key=key,
            metrics=workflow_metrics,
            pop_workflow_state=pop_workflow_state,
            pop=pop_local,
        )

    def step(self, state: State) -> Tuple[TrainMetric, State]:
        pop_workflow_state = state.pop_workflow_state
        pop = state.pop

        # ===== step ======
        def _train_steps(wf_state):
            def _one_step(wf_state, _):
                train_metrics, wf_state = self.workflow.step(wf_state)
                return wf_state, train_metrics

            wf_state, train_metrics_trajectory = jax.lax.scan(
                _one_step, wf_state, None, length=self.config.per_iter_workflow_steps
            )

            train_metrics = jtu.tree_map(
                lambda x: x[-1], train_metrics_trajectory)

            return train_metrics, wf_state

        if self.config.parallel_train:
            train_steps_fn = jax.vmap(_train_steps)
        else:
            train_steps_fn = partial(jax.lax.map, _train_steps)

        if self.enable_multi_devices:
            train_steps_fn = jax.pmap(train_steps_fn, axis_name=PMAP_AXIS_NAME)

        pop_train_metrics, pop_workflow_state = train_steps_fn(
            _train_steps, pop_workflow_state
        )

        # ===== eval ======
        if self.config.parallel_eval:
            eval_fn = jax.vmap(self.workflow.evaluate)
        else:
            eval_fn = partial(jax.lax.map, self.workflow.evaluate)

        if self.enable_multi_devices:
            eval_fn = jax.pmap(eval_fn, axis_name=PMAP_AXIS_NAME)

        pop_eval_metrics, pop_workflow_state = eval_fn(pop_workflow_state)
        pop_episode_returns = pop_eval_metrics.episode_returns

        # ===== warmup or exploit & explore ======
        key, exploit_and_explore_key = jax.random.split(state.key)

        def _dummy_fn(key, pop_episode_returns, pop, pop_workflow_state):
            return pop, pop_workflow_state

        _exploit_and_explore = partial(exploit_and_explore, self.config)

        pop, pop_workflow_state = jax.lax.cond(
            state.metrics.iterations+1 <= math.ceil(self.config.warmup_steps /
                                                    self.config.per_iter_workflow_steps),
            _dummy_fn,
            _exploit_and_explore,
            exploit_and_explore_key, pop_episode_returns, pop, pop_workflow_state
        )

        # ===== record metrics ======
        workflow_metrics = state.metrics.replace(
            sampled_timesteps=jnp.sum(
                pop_workflow_state.metrics.sampled_timesteps),
            iterations=state.metrics.iterations + 1
        )

        train_metrics = TrainMetric(
            pop_episode_returns=pop_eval_metrics.episode_returns,
            pop_episode_lengths=pop_eval_metrics.episode_lengths
        )

        return train_metrics, state.replace(
            key=key,
            metrics=workflow_metrics,
            pop=pop,
            pop_workflow_state=pop_workflow_state
        )

    def learn(self, state: State) -> State:
        for i in range(self.config.num_iters):
            train_metrics, state = self.step(state)
            workflow_metrics = state.metrics
            self.recorder.write(workflow_metrics.to_local_dict(), i)
            self.recorder.write(train_metrics.to_local_dict(), i)

            self.checkpoint_manager.save(
                i,
                args=ocp.args.StandardSave(state)
            )

        return state

    def evaluate(self, state: State) -> State:
        # Tips: evaluation consumes every workflow_state's internal key
        if self.config.parallel_eval:
            pop_eval_metrics, pop_workflow_state = jax.vmap(
                self.workflow.evaluate)(pop_workflow_state)
        else:
            pop_eval_metrics, pop_workflow_state = jax.lax.map(
                self.workflow.evaluate, pop_workflow_state
            )

        eval_metrics = EvalMetric(
            pop_episode_returns=pop_eval_metrics.episode_returns,
            pop_episode_lengths=pop_eval_metrics.episode_lengths
        )

        return eval_metrics, state.replace(pop_workflow_state=pop_workflow_state)


def exploit_and_explore(config, key, pop_episode_returns, pop, pop_workflow_state):
    bottoms_num = round(config.pop_size * config.bottom_ratio)
    tops_num = round(config.pop_size * config.top_ratio)
    indices = jnp.argsort(pop_episode_returns)
    bottoms_indices = indices[:bottoms_num]
    tops_indices = indices[-tops_num:]

    key, exploit_key, explore_key = jax.random.split(key, 3)

    def _exploit_and_explore_fn(key, top, top_wf_state):
        new = PyTreeDict()
        for hp_name in top.keys():
            new[hp_name] = top[hp_name] * (
                1+jax.random.uniform(
                    key,
                    minval=-config.perturb_factor[hp_name],
                    maxval=config.perturb_factor[hp_name])
            )
        # TODO: check deepcopy is necessary (does not change the original state)

        new_wf_state = apply_hyperparams_to_workflow_state(new, top_wf_state)
        return new, new_wf_state

    # replace bottoms with random tops
    tops_choice_indices = jax.random.choice(
        exploit_key, tops_indices, (len(bottoms_indices),))

    def _read(indices, pop):
        return jtu.tree_map(lambda x: x[indices], pop)

    def _write(indices, pop, new):
        return jtu.tree_map(lambda x, y: x.at[indices].set(y), pop, new)

    new_bottoms, new_bottoms_wf_state = jax.vmap(_exploit_and_explore_fn)(
        jax.random.split(explore_key, len(bottoms_indices)),
        _read(tops_choice_indices, pop),
        _read(tops_choice_indices, pop_workflow_state)
    )
    pop = _write(bottoms_indices, pop, new_bottoms)
    pop_workflow_state = _write(
        bottoms_indices, pop_workflow_state, new_bottoms_wf_state)

    return pop, pop_workflow_state


def apply_hyperparams_to_workflow_state(hyperparams: PyTreeDict[str, chex.Numeric], workflow_state: State):
    """
        Note1: InjectStatefulHyperparamsState is NamedTuple, which is not immutable.
        Note2: try to avoid deepcopy unnessary state
    """
    opt_state = workflow_state.opt_state
    assert isinstance(opt_state, InjectStatefulHyperparamsState)

    opt_state = deepcopy_InjectStatefulHyperparamsState(opt_state)
    opt_state.hyperparams['learning_rate'] = hyperparams.lr
    return workflow_state.replace(opt_state=opt_state)


def deepcopy_InjectStatefulHyperparamsState(state: InjectStatefulHyperparamsState):
    return InjectStatefulHyperparamsState(
        count=state.count,
        hyperparams=copy.deepcopy(state.hyperparams),
        hyperparams_states=state.hyperparams_states,
        inner_state=state.inner_state
    )


def loop_over_pmap(pmap_fn, devices, num: int, *args):
    assert num % len(
        devices) == 0, "num should be divisible by the number of devices"

    pmap_fn()
