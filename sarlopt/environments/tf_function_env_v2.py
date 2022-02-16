"""TFEnvironment for function optimization with RL using POMDP."""

import typing

import tensorflow as tf
from tensorflow.python.autograph.impl import api as autograph
from tf_agents import specs
from tf_agents.environments import tf_environment
from tf_agents.trajectories import time_step as ts
from tf_agents.utils import common, nest_utils

from optfuncs import tensorflow_functions as tff

FIRST = ts.StepType.FIRST
MID = ts.StepType.MID
LAST = ts.StepType.LAST


class TFFunctionEnvV2(tf_environment.TFEnvironment):
  """Single-agent function environment as a POMDP."""

  @autograph.do_not_convert()
  def __init__(self,
               functions: typing.List[tff.TensorflowFunction],
               dims,
               seed,
               duration: int = 50000,
               bounded_actions_spec: bool = True,
               alg=tf.random.Algorithm.PHILOX):
    self._functions = functions
    for fn in self._functions:
      fn.enable_tf_function()

    self._fn_index = common.create_variable(name='fn_index',
                                            initial_value=0,
                                            dtype=tf.int32)

    self._fn_evaluator = lambda x: tf.nest.map_structure(
      lambda f: lambda: f(x),
      self._functions)

    self._domain_min = tf.cast(
      self._functions[0].domain.min,
      tf.float32)
    self._domain_max = tf.cast(
      self._functions[0].domain.max,
      tf.float32)
    self._dims = dims
    self._n_functions = len(self._functions)

    action_spec = specs.BoundedTensorSpec(shape=(self._dims,), dtype=tf.float32,
                                          minimum=-1.0,
                                          maximum=1.0,
                                          name='action')
    if not bounded_actions_spec:
      action_spec = specs.TensorSpec.from_spec(action_spec)

    observation_spec = specs.BoundedTensorSpec(shape=(self._dims,),
                                               dtype=tf.float32,
                                               minimum=self._domain_min,
                                               maximum=self._domain_max,
                                               name='observation')

    time_step_spec = ts.time_step_spec(observation_spec)
    super().__init__(time_step_spec, action_spec)

    self._seed = seed
    self._alg = alg

    self._rng = tf.random.Generator.from_seed(self._seed, self._alg)

    self._episode_ended = common.create_variable(name='episode_ended',
                                                 initial_value=False,
                                                 dtype=tf.bool)
    self._steps_taken = common.create_variable(name='steps_taken',
                                               initial_value=0,
                                               dtype=tf.int32)
    self._duration = tf.constant(value=duration,
                                 dtype=tf.int32,
                                 name='duration')

    self._state = common.create_variable(
      name='state',
      initial_value=self._rng.uniform(
        shape=tf.TensorShape(1).concatenate(observation_spec.shape),
        minval=self._domain_min,
        maxval=self._domain_max,
        dtype=tf.float32),
      dtype=tf.float32)

    self._fn_index.assign(value=self._rng.uniform(
      shape=(),
      minval=0,
      maxval=self._n_functions,
      dtype=tf.int32))

  def _current_time_step(self) -> ts.TimeStep:
    state = self._state.value()
    fn_index = self._fn_index.value()

    with tf.control_dependencies([state]):
      branches = self._fn_evaluator(state)

    def first():
      return (tf.constant(FIRST, dtype=tf.int32),
              tf.constant(0.0, dtype=tf.float32))

    def mid():
      return (tf.constant(MID, dtype=tf.int32),
              tf.reshape(tf.math.negative(tf.switch_case(fn_index, branches)),
                         shape=()))

    def last():
      return (tf.constant(LAST, dtype=tf.int32),
              tf.reshape(tf.math.negative(tf.switch_case(fn_index, branches)),
                         shape=()))

    with tf.control_dependencies([branches]):
      discount = tf.constant(1.0, dtype=tf.float32)
      step_type, reward = tf.case(
        [(tf.math.less_equal(self._steps_taken, 0), first),
         (tf.math.reduce_any(self._episode_ended), last)],
        default=mid,
        exclusive=True, strict=True)

    return nest_utils.batch_nested_tensors(ts.TimeStep(step_type=step_type,
                                                       reward=reward,
                                                       discount=discount,
                                                       observation=state),
                                           self.time_step_spec())

  def _reset(self) -> ts.TimeStep:
    reset_ended = self._episode_ended.assign(value=False)
    reset_steps = self._steps_taken.assign(value=0)

    with tf.control_dependencies([reset_ended, reset_steps]):
      state_reset = self._state.assign(
        value=self._rng.uniform(
          shape=tf.TensorShape(1).concatenate(self.observation_spec().shape),
          minval=self._domain_min,
          maxval=self._domain_max,
          dtype=tf.float32))
      index_reset = self._fn_index.assign(
        value=self._rng.uniform(
          shape=(),
          minval=0,
          maxval=self._n_functions,
          dtype=tf.int32))

    with tf.control_dependencies([state_reset, index_reset]):
      time_step = self.current_time_step()

    return time_step

  def _step(self, action):
    action = tf.convert_to_tensor(value=action)

    def take_step():
      with tf.control_dependencies(tf.nest.flatten(action)):
        new_state = tf.clip_by_value(self._state + action,
                                     clip_value_min=self._domain_min,
                                     clip_value_max=self._domain_max)

      with tf.control_dependencies([new_state]):
        state_update = self._state.assign(new_state)
        steps_update = self._steps_taken.assign_add(1)
        episode_finished = tf.cond(
          pred=tf.math.greater_equal(self._steps_taken, self._duration),
          true_fn=lambda: self._episode_ended.assign(True),
          false_fn=self._episode_ended.value)

      with tf.control_dependencies([state_update,
                                    steps_update,
                                    episode_finished]):
        return self.current_time_step()

    def reset_env():
      return self.reset()

    return tf.cond(pred=tf.math.reduce_any(self._episode_ended),
                   true_fn=reset_env,
                   false_fn=take_step)

  @property
  @autograph.do_not_convert()
  def functions(self):
    return self._functions

  @property
  @autograph.do_not_convert()
  def fn_index(self):
    return self._fn_index

  @autograph.do_not_convert()
  def get_info(self, to_numpy=False):
    raise NotImplementedError("No info available for this environment.")

  def render(self):
    raise ValueError('Environment does not support render yet.')
