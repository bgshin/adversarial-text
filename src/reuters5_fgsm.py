import os

import numpy as np
import tensorflow as tf

from gensim.models import KeyedVectors
from gensim.similarities.index import AnnoyIndexer

from utils.core import train, evaluate, predict
from utils import Timer, tick

from attacks import fgm


os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


def model_fn(x, logits=False, training=False, name='ybar'):
    with tf.variable_scope('conv'):
        z = tf.layers.dropout(x, rate=0.2, training=training)
        z = tf.layers.conv1d(z, filters=256, kernel_size=3, padding='valid')
        z = tf.maximum(z, 0.2 * z)
        z = tf.reduce_max(z, axis=1, name='global_max_pooling')

    with tf.variable_scope('flatten'):
        shape = z.get_shape().as_list()[1:]
        z = tf.reshape(z, [-1, np.prod(shape)])

    with tf.variable_scope('mlp'):
        z = tf.layers.dense(z, units=512)
        z = tf.layers.dropout(z, rate=0.2, training=training)
        z = tf.maximum(z, 0.2 * z)

    logits_ = tf.layers.dense(z, units=5, name='logits')
    ybar = tf.nn.softmax(logits_, name=name)

    if logits:
        return ybar, logits_
    return ybar


class Dummy:
    pass


env = Dummy()

print('\nConstructing graph')

with tf.variable_scope('model'):
    env.x = tf.placeholder(tf.float32, [None, 350, 300], 'x')
    env.y = tf.placeholder(tf.float32, [None, 5], 'y')
    env.training = tf.placeholder_with_default(False, (), 'mode')

    env.ybar, logits = model_fn(env.x, logits=True, training=env.training)

    env.saver = tf.train.Saver()

    with tf.name_scope('acc'):
        t0 = tf.argmax(env.ybar, axis=1)
        t1 = tf.argmax(env.y, axis=1)
        count = tf.equal(t0, t1)
        env.acc = tf.reduce_mean(tf.cast(count, tf.float32), name='acc')

    with tf.name_scope('loss'):
        xent = tf.nn.softmax_cross_entropy_with_logits(labels=env.y,
                                                       logits=logits)
        env.loss = tf.reduce_mean(xent, name='loss')

with tf.variable_scope('model', reuse=True):
    env.adv_eps = tf.placeholder(tf.float32, (), name='adv_eps')
    env.adv_epochs = tf.placeholder(tf.int32, (), name='adv_epochs')
    env.xadv = fgm(model_fn, env.x, epochs=env.adv_epochs, eps=env.adv_eps,
                   clip_min=-100, clip_max=100)


@tick
def make_fgsm(sess, env, X_data, epochs=1, eps=0.01, batch_size=128):
    """
    Generate FGSM by running env.xadv.
    """
    print('\nMaking adversarials via FGSM')

    n_sample = X_data.shape[0]
    n_batch = int((n_sample + batch_size - 1) / batch_size)
    X_adv = np.empty_like(X_data)

    for batch in range(n_batch):
        print(' batch {0}/{1}'.format(batch + 1, n_batch), end='\r')
        start = batch * batch_size
        end = min(n_sample, start + batch_size)
        feed_dict={env.x: X_data[start:end], env.adv_eps: eps,
                   env.adv_epochs: epochs}
        adv = sess.run(env.xadv, feed_dict=feed_dict)
        X_adv[start:end] = adv
    print()

    return X_adv


print('\nInitialize session')
sess = tf.InteractiveSession()
sess.run(tf.global_variables_initializer())
sess.run(tf.local_variables_initializer())


reuters5 = os.path.expanduser('~/data/reuters/reuters5.npz')
d = np.load(reuters5)
X_train, y_train = d['X_train'], d['y_train']
X_test, y_test = d['X_test'], d['y_test']

to_categorical = tf.keras.utils.to_categorical
y_train = to_categorical(y_train).astype(np.float32)
y_test = to_categorical(y_test).astype(np.float32)

ind = np.random.permutation(X_train.shape[0])
X_train, y_train = X_train[ind], y_train[ind]

VALIDATION_SPLIT = 0.1
n = int(X_train.shape[0] * VALIDATION_SPLIT)
X_valid = X_train[:n]
X_train = X_train[n:]
y_valid = y_train[:n]
y_train = y_train[n:]

print('X_train shape: {}'.format(X_train.shape))
print('y_train shape: {}'.format(y_train.shape))
print('X_test shape: {}'.format(X_test.shape))
print('y_test shape: {}'.format(y_test.shape))

load = True
train(sess, env, X_train, y_train, X_valid, y_valid, load=load, epochs=10,
      name='reuters5')
evaluate(sess, env, X_test, y_test)

X_adv = make_fgsm(sess, env, X_test, epochs=1, eps=0.3)
evaluate(sess, env, X_adv, y_test)

with Timer():
    ifname = os.path.expanduser('~/data/glove/glove.840B.300d.w2v')
    print('\nLoading word2vec model')
    w2v = KeyedVectors.load(ifname)

print('\nInit w2v similarities')
with Timer():
    w2v.init_sims()

with Timer():
    ifname = os.path.expanduser('~/data/glove/glove.840B.300d.annoy')
    print('\nLoading index')
    annoy_index = AnnoyIndexer()
    annoy_index.load(ifname)
    annoy_index.model = w2v

X_quant = X_adv.copy()
n = len(X_quant)
sents = [''] * n
dists = np.empty(n)
cnts = np.empty(n)

print('\nQuerying with Annoy')

v0 = np.zeros(300)

def _nearest(w2v, v, v0=v0):
    if np.allclose(v, v0):
        return '<pad>', v0

    w, _ = w2v.most_similar([v], topn=1, indexer=annoy_index)[0]
    v = w2v.word_vec(w)
    return w, v

with Timer():
    for i, sent in enumerate(X_quant):
        print('{0}/{1}'.format(i+1, n), end='\r')
        org = []
        cur = []
        tmp = []
        changes, total = 0, 0
        for j, v in enumerate(sent):
            w0, _ = _nearest(w2v, X_test[i, j])

            if '<pad>' == w0:
                w1, v1 = '<pad>', v0
                w2 = w1
            else:
                total += 1
                w1, v1 = _nearest(w2v, v)
                if w1 != w0:
                    w2 = '((({0}))) [[[{1}]]]'.format(w1, w0)
                    changes += 1
                else:
                    w2 = w1

            X_quant[i, j] = v1;
            org.append(w0)
            cur.append(w1)
            tmp.append(w2)
        dists[i] = w2v.wmdistance(org, cur)
        cnts[i] = changes
        sents[i] = ('{0:.4f} {1:d} {2:.4f} '
                    .format(dists[i], changes, changes/total)
                    + ' '.join(tmp))
print()

evaluate(sess, env, X_quant, y_test)

z0 = np.argmax(y_test, axis=1)
z1 = np.argmax(predict(sess, env, X_test), axis=1)
z2 = np.argmax(predict(sess, env, X_quant), axis=1)
ind = np.where(np.logical_and(z0 == z1, z1 != z2))[0]

X_succ = ['[{0:d}-{1:d}] '.format(int(z0[i]), int(z2[i])) + sents[i]
          for i in ind]

ind = np.argsort(dists[ind])
X_succ = [X_succ[i] for i in ind]

with open('out/reuters5_fgsm.txt', 'w') as w:
    w.write('\n'.join(X_succ))

ind = np.where(np.logical_and(z0 == z1, z1 == z2))[0]
ind = np.random.choice(ind, size=100, replace=False)
X_fail = ['{:d} '.format(int(z0[i])) + sents[i] for i in ind]
ind = np.argsort(dists[ind])
X_fail = [X_fail[i] for i in ind]

with open('out/reuters5_fgsm_fail_100.txt', 'w') as w:
    w.write('\n'.join(X_fail))
