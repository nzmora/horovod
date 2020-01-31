# Copyright 2017 Uber Technologies, Inc. All Rights Reserved.
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
# ==============================================================================

import horovod.tensorflow as hvd
import tensorflow as tf


def _allreduce_gradients(gradients, _name, sparse_as_dense, device_dense, device_sparse, compression):
    if hvd.size() > 1:
        averaged_gradients = []
        with tf.name_scope(_name + "_Allreduce"):
            for grad in gradients:
                if grad is not None:
                    if sparse_as_dense and \
                            isinstance(grad, tf.IndexedSlices):
                        grad = tf.convert_to_tensor(grad)
                    avg_grad = hvd.allreduce(grad,
                                             device_dense=device_dense,
                                             device_sparse=device_sparse,
                                             compression=compression)
                    averaged_gradients.append(avg_grad)
                else:
                    averaged_gradients.append(None)
            return averaged_gradients
    else:
        return gradients


def create_distributed_optimizer(keras, optimizer, name, device_dense, device_sparse,
                                 compression, sparse_as_dense):
    class _DistributedOptimizer(keras.optimizers.Optimizer):
        def __init__(self, **kwargs):
            self._name = name or "Distributed%s" % self.__class__.__base__.__name__
            self._device_dense = device_dense
            self._device_sparse = device_sparse
            self._compression = compression
            self._sparse_as_dense = sparse_as_dense
            self._get_gradients_used = False
            super(self.__class__, self).__init__(**kwargs)

        def get_gradients(self, loss, params):
            """
            Compute gradients of all trainable variables.

            See Optimizer.get_gradients() for more info.

            In DistributedOptimizer, get_gradients() is overriden to also
            allreduce the gradients before returning them.
            """
            self._get_gradients_used = True
            gradients = super(self.__class__, self).get_gradients(loss, params)
            return _allreduce_gradients(gradients, self._name, self._sparse_as_dense,
                                        self._device_dense, self._device_sparse, self._compression)

        def apply_gradients(self, *args, **kwargs):
            if not self._get_gradients_used:
                raise Exception('`apply_gradients()` was called without a call to '
                                '`get_gradients()`. If you\'re using TensorFlow 2.0, '
                                'please specify `experimental_run_tf_function=False` in '
                                '`compile()`.')
            return super(self.__class__, self).apply_gradients(*args, **kwargs)

    # We dynamically create a new class that inherits from the optimizer that was passed in.
    # The goal is to override get_gradients() method with an allreduce implementation.
    # This class will have the same name as the optimizer it's wrapping, so that the saved
    # model could be easily restored without Horovod.
    cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
               dict(_DistributedOptimizer.__dict__))
    return cls.from_config(optimizer.get_config())


def create_distributed_optimizer_v2(keras, optimizer, name, device_dense, device_sparse,
                                    compression, sparse_as_dense):
    class _DistributedOptimizer(keras.mixed_precision.experimental.LossScaleOptimizer):
        def __init__(self, **kwargs):
            self._name = name or "Distributed%s" % self.__class__.__base__.__name__
            self._device_dense = device_dense
            self._device_sparse = device_sparse
            self._compression = compression
            self._sparse_as_dense = sparse_as_dense
            self.get_unscaled_gradients_called = False
            super(self.__class__, self).__init__(**kwargs)

        def get_unscaled_gradients(self, *args, **kwargs):
            """
            Unscales the gradients by the loss scale.

            See LossScaleOptimizer.get_unscaled_gradients() for more info.

            In DistributedOptimizer, get_unscaled_gradients() is overriden to
            allreduce the gradients before returning them.
            """
            self.get_unscaled_gradients_called = True
            gradients = super(self.__class__, self).get_unscaled_gradients(*args, **kwargs)
            return _allreduce_gradients(gradients, self._name, self._sparse_as_dense,
                                        self._device_dense, self._device_sparse, self._compression)
        
        def apply_gradients(self, *args, **kwargs):
            if not self.get_unscaled_gradients_called:
                raise Exception('`apply_gradients()` was called without a call to '
                                '`get_unscaled_gradients()`.')
            return super(self.__class__, self).apply_gradients(*args, **kwargs)

    # If optimizer is not an instance of LossScaleOptimizer, we dynamically create a new
    # class that inherits from the optimizer that was passed in. The goal is to override
    # get_unscaled_gradients() method with an allreduce implementation.
    # This class will have the same name as the optimizer it's wrapping, so that the saved
    # model could be easily restored without Horovod.
    #
    # However, if the optimizer was not an instance of LossScaleOptimizer, we would wrap it
    # with a LossScaleOptimizer and set the scaling factor to 1.0. This does not have any
    # effect on gradient calculation but enables us to inject allreduce operation before
    # getting all the gradients.
    if not isinstance(optimizer, keras.mixed_precision.experimental.LossScaleOptimizer):
        optimizer = keras.mixed_precision.experimental.LossScaleOptimizer(optimizer, 1.0)
    cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
               dict(_DistributedOptimizer.__dict__))

    return cls.from_config(optimizer.get_config())


def _eval(backend, op_or_result):
    if hvd._executing_eagerly():
        return op_or_result
    else:
        return backend.get_session().run(op_or_result)


if hasattr(hvd, 'broadcast_global_variables'):
    def broadcast_global_variables(backend, root_rank):
        return _eval(backend, hvd.broadcast_global_variables(root_rank))


def allreduce(backend, value, name, average):
    return _eval(backend, hvd.allreduce(tf.constant(value, name=name), average=average))


def allgather(backend, value, name):
    return _eval(backend, hvd.allgather(tf.constant(value, name=name)))


def broadcast(backend, value, root_rank, name):
    return _eval(backend, hvd.broadcast(tf.constant(value, name=name), root_rank))


def load_model(keras, wrap_optimizer, filepath, custom_optimizers, custom_objects):
    horovod_objects = {
        subclass.__name__.lower(): wrap_optimizer(subclass)
        for subclass in keras.optimizers.Optimizer.__subclasses__()
        if subclass.__module__ == keras.optimizers.Optimizer.__module__
    }

    if custom_optimizers is not None:
        horovod_objects.update({
            cls.__name__: wrap_optimizer(cls)
            for cls in custom_optimizers
        })

    if custom_objects is not None:
        horovod_objects.update(custom_objects)

    return keras.models.load_model(filepath, custom_objects=horovod_objects)
