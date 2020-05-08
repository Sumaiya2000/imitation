"""Behavioural Cloning (BC).

Trains policy by applying supervised learning to a fixed dataset of (observation,
action) pairs generated by some expert demonstrator.
"""

import os
from typing import Callable, List, Optional, Type

import cloudpickle
import numpy as np
import tensorflow as tf
from stable_baselines.common.dataset import Dataset
from stable_baselines.common.policies import ActorCriticPolicy, BasePolicy
from tqdm.autonotebook import tqdm, trange

from imitation.data import rollout, types
from imitation.policies.base import FeedForward32Policy


def set_tf_vars(
    *,
    values: List[np.ndarray],
    scope: Optional[str] = None,
    tf_vars: Optional[List[tf.Variable]] = None,
    sess: Optional[tf.Session] = None,
):
    """Set a collection of variables to take the values in `values`.

    Variables can be either specified by scope or passed directly into the
    function as a list. Variables and values will be matched based on the order
    in which they appear in their respective collections, so there must be as
    many values as variables.

    Args:
        values: list of values to load into variables.
        scope: scope to collect variables from. Either this argument xor
          `tf_vars` must be given.
        tf_vars: explicit list of TF variables to write to. Mutex with `scope`.
        sess: TF session to use, if not the default.
    """
    if scope is not None:
        assert tf_vars is None, "must give either `tf_vars` xor `scope` kwargs"
        tf_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope)
    else:
        assert tf_vars is not None, "must give either `tf_vars` xor `scope` kwargs"
    assert len(tf_vars) == len(values), (
        f"{len(tf_vars)} tf variables but " f"{len(values)} values supplied"
    )
    sess = sess or tf.get_default_session()
    assert sess is not None, "must supply session or have one in context"
    placeholders = [tf.placeholder(shape=v.shape, dtype=v.dtype) for v in tf_vars]
    assign_ops = [tf.assign(var, ph) for var, ph in zip(tf_vars, placeholders)]
    sess.run(
        assign_ops, feed_dict={ph: value for ph, value in zip(placeholders, values)}
    )


class BCTrainer:
    """Simple behavioural cloning (BC).

    Recovers only a policy.

    Args:
      env: environment to train on.
      expert_rollouts: A tuple of four arrays from expert rollouts, `obs`, `act`,
          `next_obs`, `reward`.
      policy_class: used to instantiate imitation policy.
      batch_size: batch size used for training.
      optimiser_cls: optimiser to use for supervised training.
      optimiser_kwargs: keyword arguments to pass to optimiser when constructing
          it.
    """

    def __init__(
        self,
        env,
        *,
        expert_demos: types.Transitions,
        policy_class: Type[ActorCriticPolicy] = FeedForward32Policy,
        batch_size: int = 32,
        optimiser_cls: Type[tf.train.Optimizer] = tf.train.AdamOptimizer,
        optimiser_kwargs: Optional[dict] = None,
        name_scope: Optional[str] = None,
        reuse: bool = False,
    ):
        self.env = env
        self.policy_class = policy_class
        self.batch_size = batch_size
        if expert_demos is not None:
            self.set_expert_dataset(expert_demos)
        else:
            self.expert_dataset = None
        self.sess = tf.get_default_session()
        assert self.sess is not None, "need to construct this within a session scope"
        self._build_tf_graph()
        self.sess.run(tf.global_variables_initializer())

    def set_expert_dataset(self, expert_demos: types.Transitions):
        """Replace the current expert dataset with a new one.

        Useful for DAgger and other interactive algorithms.

        Args:
          expert_rollouts: A tuple of four arrays from expert rollouts,
              `obs`, `act`, `next_obs`, `reward`.
        """
        self.expert_dataset = Dataset(
            {"obs": expert_demos.obs, "act": expert_demos.acts}, shuffle=True
        )

    def train(
        self, *, n_epochs: int = 100, on_epoch_end: Callable[[dict], None] = None
    ):
        """Train with supervised learning for some number of epochs.

        Here an 'epoch' is just a complete pass through the expert transition
        dataset.

        Args:
          n_epochs: number of complete passes made through dataset.
          on_epoch_end: optional callback to run at
            the end of each epoch. Will receive all locals from this function as
            dictionary argument (!!).
        """
        epoch_iter = trange(n_epochs, desc="BC epoch")
        for epoch_num in epoch_iter:
            total_batches = self.expert_dataset.n_samples // self.batch_size
            batch_iter = self.expert_dataset.iterate_once(self.batch_size)
            tq_iter = tqdm(
                batch_iter, total=total_batches, desc="pol step", leave=False
            )
            loss_ewma = None
            for batch_dict in tq_iter:
                feed_dict = {
                    self._true_acts_ph: batch_dict["act"],
                    self.policy.obs_ph: batch_dict["obs"],
                }
                _, loss = self.sess.run(
                    [self._train_op, self._log_loss], feed_dict=feed_dict
                )
                tq_iter.set_postfix(loss="% 3.4f" % loss)
                if loss_ewma is None:
                    loss_ewma = loss
                else:
                    loss_ewma = 0.9 * loss_ewma + 0.1 * loss
            epoch_iter.set_postfix(loss_ewma="% 3.4f" % loss_ewma)
            if on_epoch_end is not None:
                on_epoch_end(locals())

    def test_policy(self, *, min_episodes: int = 10) -> dict:
        """Test current imitation policy on environment & give some rollout stats.

        Args:
          min_episodes: Minimum number of rolled-out episodes.

        Returns:
          rollout statistics collected by `imitation.utils.rollout.rollout_stats()`.
        """
        trajs = rollout.generate_trajectories(
            self.policy, self.env, sample_until=rollout.min_episodes(min_episodes)
        )
        reward_stats = rollout.rollout_stats(trajs)
        return reward_stats

    def _build_tf_graph(self):
        with tf.variable_scope("bc_supervised_loss"):
            with tf.variable_scope("model"):
                self.policy_kwargs = dict(
                    ob_space=self.env.observation_space,
                    ac_space=self.env.action_space,
                    n_batch=None,
                    n_env=1,
                    n_steps=1000,
                )
                self.policy = self.policy_class(
                    sess=self.sess, **self.policy_kwargs
                )  # pytype: disable=not-instantiable
                inner_scope = tf.get_variable_scope().name
                self.policy_variables = tf.get_collection(
                    tf.GraphKeys.TRAINABLE_VARIABLES, scope=inner_scope
                )
            self._true_acts_ph = self.policy.pdtype.sample_placeholder(
                [None], name="ref_acts_ph"
            )
            self._log_loss = tf.reduce_mean(
                self.policy.proba_distribution.neglogp(self._true_acts_ph)
            )
            # FIXME: it should be possible to customise both optimiser class and
            # optimiser arguments
            opt = tf.train.AdamOptimizer()
            self._train_op = opt.minimize(self._log_loss)

    def save_policy(self, policy_path: str):
        """Save a policy to a pickle. Can be reloaded by `.reconstruct_policy()`.

        Args:
            policy_path: path to save policy to.
        """
        policy_params = self.sess.run(self.policy_variables)
        data = {
            "class": self.policy_class,
            "kwargs": self.policy_kwargs,
            "params": policy_params,
        }
        dirname = os.path.dirname(policy_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(policy_path, "wb") as fp:
            cloudpickle.dump(data, fp)

    @staticmethod
    def reconstruct_policy(
        policy_path: str, sess: Optional[tf.Session] = None,
    ) -> BasePolicy:
        """Reconstruct a saved policy.

        Args:
            policy_path: path a policy produced by `.save_policy()`.
            sess: optional session to construct policy under,
              if not the default session.

        Returns:
            policy: policy with reloaded weights.
        """
        if sess is None:
            sess = tf.get_default_session()
            assert sess is not None, "must supply session via kwarg or context mgr"

        # re-read data from dict
        with open(policy_path, "rb") as fp:
            loaded_pickle = cloudpickle.load(fp)

        # construct the policy class
        klass = loaded_pickle["class"]
        kwargs = loaded_pickle["kwargs"]
        with tf.variable_scope("reconstructed_policy"):
            rv_pol = klass(sess=sess, **kwargs)
            inner_scope = tf.get_variable_scope().name

        # set values for the new policy's parameters
        param_values = loaded_pickle["params"]
        set_tf_vars(values=param_values, scope=inner_scope, sess=sess)

        return rv_pol
