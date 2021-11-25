"""TD3 para aprender um algoritmo de otimização."""

import time

import numpy as np
import tensorflow as tf
from tf_agents.agents.td3 import td3_agent
from tf_agents.agents.ddpg import actor_rnn_network as actor_rnn_net
from tf_agents.agents.ddpg import critic_rnn_network as critic_rnn_net
from tf_agents.drivers import dynamic_step_driver as dy_sd
from tf_agents.drivers import dynamic_episode_driver as dy_ed
from tf_agents.environments import tf_py_environment
from tf_agents.environments import wrappers
from tf_agents.replay_buffers import tf_uniform_replay_buffer
from tf_agents.train.utils import train_utils
from tf_agents.utils import common
from tf_agents.metrics import tf_metrics

from optfuncs import numpy_functions as npf
from optfuncs import tensorflow_functions as tff
from optfuncs import core as functions_core

from src.environments import py_function_environment as py_fun_env
from src.metrics import tf_custom_metrics
from src.typing.types import LayerParam

from experiments.evaluation import utils as eval_utils
from experiments.training import utils as training_utils


def train_td3_rnn(function: functions_core.Function,
                  dims: int,
                  training_episodes: int = 200,
                  train_sequence_length: int = 75,
                  stop_threshold: float = None,
                  env_steps: int = 100,
                  env_eval_steps: int = 150,
                  eval_interval: int = 25,
                  eval_episodes: int = 5,
                  initial_collect_episodes: int = 10,
                  collect_steps_per_iteration: int = 1,
                  buffer_size: int = 1000000,
                  batch_size: int = 64,
                  actor_lr: float = 1e-4,
                  critic_lr: float = 1e-3,
                  tau: float = 5e-2,
                  actor_update_period: int = 2,
                  target_update_period: int = 1,
                  discount: float = 0.995,
                  exploration_noise_std: float = 0.15,
                  target_policy_noise: float = 0.2,
                  target_policy_noise_clip: float = 0.5,
                  actor_layers: LayerParam = None,
                  actor_lstm_size: LayerParam = None,
                  critic_action_layers: LayerParam = None,
                  critic_observation_layers: LayerParam = None,
                  critic_joint_layers: LayerParam = None,
                  critic_lstm_size: LayerParam = None,
                  summary_flush_secs: int = 10,
                  debug_summaries: bool = False,
                  summarize_grads_and_vars: bool = False):
  algorithm_name = 'TD3-RNN'

  # Criando o diretório do agente
  agent_dir = training_utils.create_agent_dir(algorithm_name,
                                              function,
                                              dims)

  # Obtendo função equivalente em TensorFlow (Utilizada no cálculo das métricas)
  tf_function = npf.get_tf_function(function)

  def grad_fn(x: np.ndarray):
    t_x = tf.convert_to_tensor(x, dtype=x.dtype)
    grad, _ = tff.get_grads(tf_function, t_x)
    return grad.numpy()

  env_training = py_fun_env.PyFunctionEnvV1(function=function,
                                            dims=dims,
                                            grad_fun=grad_fn)

  env_training = wrappers.TimeLimit(env=env_training, duration=env_steps)
  env_training = wrappers.FlattenObservationsWrapper(env=env_training)

  env_eval = py_fun_env.PyFunctionEnvV1(function=function,
                                        dims=dims,
                                        grad_fun=grad_fn)
  env_eval = wrappers.TimeLimit(env=env_eval, duration=env_eval_steps)
  env_eval = wrappers.FlattenObservationsWrapper(env=env_eval)

  # Conversão para TFPyEnvironment's
  tf_env_training = tf_py_environment.TFPyEnvironment(environment=env_training)
  tf_env_eval = tf_py_environment.TFPyEnvironment(environment=env_eval)

  # Criação dos SummaryWriter's
  print('Creating logs directories.')
  log_dir, log_eval_dir, log_train_dir = training_utils.create_logs_dir(
    agent_dir)

  train_summary_writer = tf.compat.v2.summary.create_file_writer(
    log_train_dir, flush_millis=summary_flush_secs * 1000)
  train_summary_writer.set_as_default()

  eval_summary_writer = tf.compat.v2.summary.create_file_writer(
    log_eval_dir, flush_millis=summary_flush_secs * 1000)

  # Criação das métricas
  train_metrics = [tf_metrics.AverageReturnMetric(),
                   tf_metrics.MaxReturnMetric()]

  eval_metrics = [tf_metrics.AverageReturnMetric(buffer_size=eval_episodes)]

  # Criação do agente, redes neurais, otimizadores
  time_spec = tf_env_training.time_step_spec()
  obs_spec = time_spec.observation
  act_spec = tf_env_training.action_spec()

  if actor_layers is None:
    actor_layers = [256, 256]

  if actor_lstm_size is None:
    actor_lstm_size = [40]

  actor_activation_fn = tf.keras.activations.relu

  actor_network = actor_rnn_net.ActorRnnNetwork(
    input_tensor_spec=obs_spec,
    output_tensor_spec=act_spec,
    lstm_size=actor_lstm_size,
    output_fc_layer_params=actor_layers,
    activation_fn=actor_activation_fn)

  if critic_joint_layers is None:
    critic_joint_layers = [256, 256]

  if critic_lstm_size is None:
    critic_lstm_size = [40]

  critic_activation_fn = tf.keras.activations.relu
  critic_output_activation_fn = tf.keras.activations.linear

  critic_network = critic_rnn_net.CriticRnnNetwork(
    input_tensor_spec=(obs_spec, act_spec),
    output_fc_layer_params=critic_joint_layers,
    lstm_size=critic_lstm_size)

  actor_optimizer = tf.keras.optimizers.Adam(learning_rate=actor_lr)
  critic_optimizer = tf.keras.optimizers.Adam(learning_rate=critic_lr)

  train_step = train_utils.create_train_step()

  agent = td3_agent.Td3Agent(
    time_step_spec=time_spec,
    action_spec=act_spec,
    actor_network=actor_network,
    critic_network=critic_network,
    actor_optimizer=actor_optimizer,
    critic_optimizer=critic_optimizer,
    target_update_tau=tau,
    exploration_noise_std=exploration_noise_std,
    target_policy_noise=target_policy_noise,
    target_policy_noise_clip=target_policy_noise_clip,
    actor_update_period=actor_update_period,
    target_update_period=target_update_period,
    train_step_counter=train_step,
    gamma=discount,
    debug_summaries=debug_summaries,
    summarize_grads_and_vars=summarize_grads_and_vars)

  agent.initialize()

  # Criação do Replay Buffer e drivers
  replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
    data_spec=agent.collect_data_spec,
    batch_size=tf_env_training.batch_size,
    max_length=buffer_size)

  observers_train = [replay_buffer.add_batch] + train_metrics
  driver = dy_sd.DynamicStepDriver(env=tf_env_training,
                                   policy=agent.collect_policy,
                                   observers=observers_train,
                                   num_steps=collect_steps_per_iteration)

  initial_collect_driver = dy_ed.DynamicEpisodeDriver(
    env=tf_env_training,
    policy=agent.collect_policy,
    observers=[replay_buffer.add_batch],
    num_episodes=initial_collect_episodes)

  eval_driver = dy_ed.DynamicEpisodeDriver(env=tf_env_eval,
                                           policy=agent.policy,
                                           observers=eval_metrics,
                                           num_episodes=eval_episodes)

  # Conversão das principais funções para tf.function's
  initial_collect_driver.run = common.function(initial_collect_driver.run)
  driver.run = common.function(driver.run)
  eval_driver.run = common.function(eval_driver.run)
  agent.train = common.function(agent.train)

  print('Initializing replay buffer by collecting experience for {0} '
        'episodes with a collect policy.'.format(initial_collect_episodes))
  initial_collect_driver.run()

  # Criação do dataset
  dataset = replay_buffer.as_dataset(
    num_parallel_calls=3,
    sample_batch_size=batch_size,
    num_steps=train_sequence_length + 1).prefetch(3)

  iterator = iter(dataset)

  # Criação da função para calcular as métricas
  def compute_eval_metrics():
    return eval_utils.eager_compute(eval_metrics,
                                    eval_driver,
                                    train_step=agent.train_step_counter,
                                    summary_writer=eval_summary_writer,
                                    summary_prefix='Metrics')

  agent.train_step_counter.assign(0)

  @tf.function
  def train_phase():
    print('tracing')
    driver.run()
    experience, _ = next(iterator)
    agent.train(experience)

  # Salvando hiperparâmetros antes de iniciar o treinamento
  hp_dict = {
    "discount": discount,
    "exploration_noise_std": exploration_noise_std,
    "tau": tau,
    "target_update_period": target_update_period,
    "actor_update_period": actor_update_period,
    "training_episodes": training_episodes,
    "buffer_size": buffer_size,
    "batch_size": batch_size,
    "stop_threshold": stop_threshold,
    "train_env": {
      "steps": env_steps,
      "function": function.name,
      "dims": dims,
      "domain": function.domain
    },
    "eval_env": {
      "steps": env_eval_steps,
      "function": function.name,
      "dims": dims,
      "domain": function.domain
    },
    "networks": {
      "actor_net": {
        "class": type(actor_network).__name__,
        "activation_fn": actor_activation_fn.__name__,
        "actor_layers": actor_layers
      },
      "critic_net": {
        "class": type(critic_network).__name__,
        "activation_fn": critic_activation_fn.__name__,
        "output_activation_fn": critic_output_activation_fn.__name__,
        "critic_action_fc_layers": critic_action_layers,
        "critic_obs_fc_layers": critic_observation_layers,
        "critic_joint_layers": critic_joint_layers
      }
    },
    "optimizers": {
      "actor_optimizer": type(actor_optimizer).__name__,
      "actor_lr": actor_lr,
      "critic_optimizer": type(critic_optimizer).__name__,
      "critic_lr": critic_lr
    }
  }

  training_utils.save_specs(agent_dir, hp_dict)
  tf.summary.text("Hyperparameters",
                  training_utils.json_pretty_string(hp_dict),
                  step=0)

  # Treinamento
  for ep in range(training_episodes):
    start_time = time.time()
    for _ in range(env_steps):
      train_phase()

      for train_metric in train_metrics:
        train_metric.tf_summaries(train_step=agent.train_step_counter)

    if ep % eval_interval == 0:
      print('-------- Evaluation --------')
      start_eval = time.time()
      results = compute_eval_metrics()
      avg_return = results.get(eval_metrics[0].name)
      print('Average return: {0}'.format(avg_return))
      print('Eval delta time: {0:.2f}'.format(time.time() - start_eval))
      print('---------------------------')

    delta_time = time.time() - start_time
    print('Finished episode {0}. '
          'Delta time since last episode: {1:.2f}'.format(ep, delta_time))

  # Computando métricas de avaliação uma última vez.
  compute_eval_metrics()

  # Salvamento da policy aprendida.
  # Pasta de saída: output/TD3-{dims}D-{function.name}-{num}/policy
  training_utils.save_policy(agent_dir, agent.policy)


if __name__ == '__main__':
  train_td3_rnn(npf.SumSquares(), 2)
