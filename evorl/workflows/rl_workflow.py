import copy
import logging
from typing import Any

import chex
import jax
import optax
from omegaconf import DictConfig, OmegaConf
from typing_extensions import Self  # pytype: disable=not-supported-yet

from evorl.agent import Agent, AgentState
from evorl.distributed import PMAP_AXIS_NAME, split_key_to_devices
from evorl.envs import Env
from evorl.evaluator import Evaluator
from evorl.metrics import EvaluateMetric, MetricBase, WorkflowMetric
from evorl.types import State

from .workflow import Workflow

# from evorl.types import State


logger = logging.getLogger(__name__)


class RLWorkflow(Workflow):
    def __init__(self, config: DictConfig):
        """
        config:
        devices: a single device or a list of devices.
        """
        super().__init__(config)

        self.pmap_axis_name = None
        self.devices = jax.local_devices()[:1]

    @property
    def enable_multi_devices(self) -> bool:
        return self.pmap_axis_name is not None

    @classmethod
    def build_from_config(
        cls,
        config: DictConfig,
        enable_multi_devices: bool = False,
        enable_jit: bool = True,
    ) -> Self:
        config = copy.deepcopy(config)  # avoid in-place modification

        devices = jax.local_devices()

        if enable_multi_devices:
            cls.enable_pmap(PMAP_AXIS_NAME)
            OmegaConf.set_readonly(config, False)
            cls._rescale_config(config)
        elif enable_jit:
            cls.enable_jit()

        OmegaConf.set_readonly(config, True)

        workflow = cls._build_from_config(config)
        if enable_multi_devices:
            workflow.pmap_axis_name = PMAP_AXIS_NAME
            workflow.devices = devices

        return workflow

    @classmethod
    def _build_from_config(cls, config: DictConfig) -> Self:
        raise NotImplementedError

    @staticmethod
    def _rescale_config(config: DictConfig) -> None:
        """
        When enable_multi_devices=True, rescale config settings in-place to match multi-devices
        """
        pass

    def step(self, state: State) -> tuple[MetricBase, State]:
        raise NotImplementedError

    def evaluate(self, state: State) -> tuple[MetricBase, State]:
        raise NotImplementedError

    @classmethod
    def enable_jit(cls) -> None:
        cls.evaluate = jax.jit(cls.evaluate, static_argnums=(0,))
        cls.step = jax.jit(cls.step, static_argnums=(0,))

    @classmethod
    def enable_pmap(cls, axis_name) -> None:
        cls.step = jax.pmap(cls.step, axis_name, static_broadcasted_argnums=(0,))
        cls.evaluate = jax.pmap(
            cls.evaluate, axis_name, static_broadcasted_argnums=(0,)
        )


class OnPolicyWorkflow(RLWorkflow):
    def __init__(
        self,
        env: Env,
        agent: Agent,
        optimizer: optax.GradientTransformation,
        evaluator: Evaluator,
        config: DictConfig,
    ):
        super().__init__(config)

        self.env = env
        self.agent = agent
        self.optimizer = optimizer
        self.evaluator = evaluator

    def _setup_agent_and_optimizer(
        self, key: chex.PRNGKey
    ) -> tuple[AgentState, chex.ArrayTree]:
        agent_state = self.agent.init(self.env.obs_space, self.env.action_space, key)
        opt_state = self.optimizer.init(agent_state.params)
        return agent_state, opt_state

    def _setup_workflow_metrics(self) -> MetricBase:
        return WorkflowMetric()

    def setup(self, key: chex.PRNGKey) -> State:
        key, agent_key, env_key = jax.random.split(key, 3)

        agent_state, opt_state = self._setup_agent_and_optimizer(agent_key)
        workflow_metrics = self._setup_workflow_metrics()

        if self.enable_multi_devices:
            workflow_metrics, agent_state, opt_state = jax.device_put_replicated(
                (workflow_metrics, agent_state, opt_state), self.devices
            )

            # key and env_state should be different over devices
            key = split_key_to_devices(key, self.devices)

            env_key = split_key_to_devices(env_key, self.devices)
            env_state = jax.pmap(self.env.reset, axis_name=self.pmap_axis_name)(env_key)
        else:
            env_state = self.env.reset(env_key)

        return State(
            key=key,
            metrics=workflow_metrics,
            agent_state=agent_state,
            env_state=env_state,
            opt_state=opt_state,
        )

    def evaluate(self, state: State) -> tuple[MetricBase, State]:
        key, eval_key = jax.random.split(state.key, num=2)

        # [#episodes]
        raw_eval_metrics = self.evaluator.evaluate(
            state.agent_state, num_episodes=self.config.eval_episodes, key=eval_key
        )

        eval_metrics = EvaluateMetric(
            episode_returns=raw_eval_metrics.episode_returns.mean(),
            episode_lengths=raw_eval_metrics.episode_lengths.mean(),
        ).all_reduce(pmap_axis_name=self.pmap_axis_name)

        state = state.replace(key=key)
        return eval_metrics, state


class OffPolicyWorkflow(RLWorkflow):
    def __init__(
        self,
        env: Env,
        agent: Agent,
        optimizer: optax.GradientTransformation,
        evaluator: Evaluator,
        replay_buffer: Any,
        config: DictConfig,
    ):
        super().__init__(config)

        self.env = env
        self.agent = agent
        self.optimizer = optimizer
        self.evaluator = evaluator
        self.replay_buffer = replay_buffer

    def _setup_agent_and_optimizer(
        self, key: chex.PRNGKey
    ) -> tuple[AgentState, chex.ArrayTree]:
        agent_state = self.agent.init(self.env.obs_space, self.env.action_space, key)
        opt_state = self.optimizer.init(agent_state.params)
        return agent_state, opt_state

    def _setup_workflow_metrics(self) -> MetricBase:
        return WorkflowMetric()

    def _setup_replaybuffer(self, key: chex.PRNGKey) -> chex.ArrayTree:
        raise NotImplementedError

    def _postsetup_replaybuffer(self, state: State) -> State:
        raise NotImplementedError

    def setup(self, key: chex.PRNGKey) -> State:
        key, agent_key, env_key, rb_key = jax.random.split(key, 4)

        agent_state, opt_state = self._setup_agent_and_optimizer(agent_key)
        workflow_metrics = self._setup_workflow_metrics()

        if self.enable_multi_devices:
            workflow_metrics, agent_state, opt_state = jax.device_put_replicated(
                (workflow_metrics, agent_state, opt_state), self.devices
            )

            # key and env_state should be different over devices
            key = split_key_to_devices(key, self.devices)

            env_key = split_key_to_devices(env_key, self.devices)
            env_state = jax.pmap(self.env.reset, axis_name=self.pmap_axis_name)(env_key)
            rb_key = split_key_to_devices(rb_key, self.devices)
            replay_buffer_state = jax.pmap(
                self._setup_replaybuffer, axis_name=self.pmap_axis_name
            )(rb_key)
        else:
            env_state = self.env.reset(env_key)
            replay_buffer_state = self._setup_replaybuffer(rb_key)

        state = State(
            key=key,
            metrics=workflow_metrics,
            agent_state=agent_state,
            env_state=env_state,
            opt_state=opt_state,
            replay_buffer_state=replay_buffer_state,
        )

        logger.info("Start replay buffer post-setup")
        if self.enable_multi_devices:
            state = jax.pmap(
                self._postsetup_replaybuffer, axis_name=self.pmap_axis_name
            )(state)
        else:
            state = self._postsetup_replaybuffer(state)

        logger.info("Complete replay buffer post-setup")

        return state

    def evaluate(self, state: State) -> tuple[MetricBase, State]:
        key, eval_key = jax.random.split(state.key, num=2)

        # [#episodes]
        raw_eval_metrics = self.evaluator.evaluate(
            state.agent_state, num_episodes=self.config.eval_episodes, key=eval_key
        )

        eval_metrics = EvaluateMetric(
            episode_returns=raw_eval_metrics.episode_returns.mean(),
            episode_lengths=raw_eval_metrics.episode_lengths.mean(),
        ).all_reduce(pmap_axis_name=self.pmap_axis_name)

        state = state.replace(key=key)
        return eval_metrics, state
