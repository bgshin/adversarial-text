import os

import numpy as np
import tensorflow as tf

from gensim.models import KeyedVectors
from gensim.similarities.index import AnnoyIndexer

from utils.core import train, evaluate, predict
from utils import Timer, tick

from attacks import deepfool


os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


DATA = 'imdb'                     # dataset
ALGO = 'deepfool'                 # attacking method
MODEL = 'imdb-1'                  # IMDB -1/1 class
DATAPATH = os.path.expanduser('~/data/imdb/imdb.npz')  # preprocessed data

MAXLEN = 400                    # max sentence length
DIM = 300                       # embedding dimension
NCLASS = 1                      # output dimension


# Tunable parameters
EPS = 30                        # deepfool eps
EPOCHS = 10                     # deepfool epochs


def model_fn(x, logits=False, training=False, name='ybar'):
    with tf.variable_scope('conv'):
        z = tf.layers.dropout(x, rate=0.2, training=training)
        z = tf.layers.conv1d(z, filters=128, kernel_size=3, padding='valid')
        z = tf.maximum(z, 0.2 * z)
        z = tf.reduce_max(z, axis=1, name='global_max_pooling')

    with tf.variable_scope('flatten'):
        shape = z.get_shape().as_list()[1:]
        z = tf.reshape(z, [-1, np.prod(shape)])

    with tf.variable_scope('mlp'):
        z = tf.layers.dense(z, units=256)
        z = tf.layers.dropout(z, rate=0.2, training=training)
        z = tf.maximum(z, 0.2 * z)

    logits_ = tf.layers.dense(z, units=1, name='logits')
    ybar = tf.tanh(logits_, name=name)

    if logits:
        return ybar, logits_
    return ybar


class Dummy:
    pass


env = Dummy()

print('\nConstructing graph')

with tf.variable_scope('model'):
    env.x = tf.placeholder(tf.float32, [None, MAXLEN, DIM], 'x')
    env.y = tf.placeholder(tf.float32, [None, NCLASS], 'y')
    env.training = tf.placeholder_with_default(False, (), 'mode')

    env.ybar, logits = model_fn(env.x, logits=True, training=env.training)

    env.saver = tf.train.Saver()

    with tf.name_scope('acc'):
        t0 = tf.greater(env.ybar, 0)
        t1 = tf.greater(env.y, 0)
        count = tf.equal(t0, t1)
        env.acc = tf.reduce_mean(tf.cast(count, tf.float32), name='acc')

    env.loss = tf.losses.mean_squared_error(labels=env.y,
                                            predictions=env.ybar,
                                            scope='loss')

    with tf.name_scope('train_op'):
        optimizer = tf.train.AdamOptimizer()
        env.train_op = optimizer.minimize(env.loss)

with tf.variable_scope('model', reuse=True):
    env.adv_epochs = tf.placeholder(tf.int32, (), name='adv_epochs')
    env.noise = deepfool(model_fn, env.x, epochs=env.adv_epochs, noise=True,
                         batch=True, clip_min=-10, clip_max=10)


@tick
def make_deepfool(sess, env, X_data, epochs=1, batch_size=128):
    """
    Generate DeepFool by running env.xadv.
    """
    print('\nMaking adversarials via DeepFool')

    n_sample = X_data.shape[0]
    n_batch = int((n_sample + batch_size - 1) / batch_size)
    X_noise = np.empty_like(X_data)

    for batch in range(n_batch):
        print(' batch {0}/{1}'.format(batch + 1, n_batch), end='\r')
        start = batch * batch_size
        end = min(n_sample, start + batch_size)
        noise = sess.run(env.noise, feed_dict={env.x: X_data[start:end],
                                               env.adv_epochs: epochs})
        X_noise[start:end] = noise
    print()

    return X_noise


with Timer('\nInitialize session'):
    sess = tf.InteractiveSession()
    sess.run(tf.global_variables_initializer())
    sess.run(tf.local_variables_initializer())


with Timer('\nLoading data'):
    d = np.load(DATAPATH)
    X_train, y_train = d['X_train'], d['y_train']
    X_test, y_test = d['X_test'], d['y_test']

    y_train = np.expand_dims(y_train, axis=1).astype(np.float32)
    y_test = np.expand_dims(y_test, axis=1).astype(np.float32)

    y_train = y_train * 2 - 1
    y_test = y_test * 2 - 1

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


train(sess, env, X_train, y_train, X_valid, y_valid, load=True, epochs=10,
      name=MODEL)
evaluate(sess, env, X_test, y_test)


X_noise = make_deepfool(sess, env, X_test, epochs=EPOCHS)

# X_rnd = np.random.random(X_noise.shape) * 2 - 1
X_adv = X_test + EPS*X_noise
evaluate(sess, env, X_adv, y_test)


with Timer('\nLoading word2vec'):
    ifname = os.path.expanduser('~/data/glove/glove.840B.300d.w2v')
    w2v = KeyedVectors.load(ifname)

with Timer('\nInit w2v similarities'):
    w2v.init_sims()

with Timer('\nLoading index'):
    ifname = os.path.expanduser('~/data/glove/glove.840B.300d.annoy')
    annoy_index = AnnoyIndexer()
    annoy_index.load(ifname)
    annoy_index.model = w2v

v0 = np.zeros(300)

def _nearest(w2v, v, v0=v0):
    if np.allclose(v, v0):
        return '<pad>', v0

    w, _ = w2v.most_similar([v], topn=1, indexer=annoy_index)[0]
    v = w2v.word_vec(w)
    return w, v


with Timer('\nQuerying with Annoy'):
    X_quant = X_adv.copy()
    n = len(X_quant)
    sents = [''] * n
    dists = np.empty(n)
    cnts = np.empty(n)
    lens = np.empty(n)

    for i, sent in enumerate(X_quant):
        print('{0}/{1}'.format(i+1, n), end='\r')
        org = []
        cur = []
        tmp = []
        change, total = 0, 0
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
                    change += 1
                else:
                    w2 = w1

            X_quant[i, j] = v1;
            org.append(w0)
            cur.append(w1)
            tmp.append(w2)
        dists[i] = w2v.wmdistance(org, cur)
        cnts[i] = change
        sents[i] = ('{0:.4f} {1:d} {2:.4f} '
                    .format(dists[i], change, change/total)
                    + ' '.join(tmp))
        lens[i] = total
    print()

evaluate(sess, env, X_quant, y_test)

z0 = y_test.flatten() > 0
z1 = predict(sess, env, X_test).flatten() > 0
z2 = predict(sess, env, X_quant).flatten() > 0
ind = np.where(np.logical_and(z0 == z1, z0 != z2))[0]

X_succ = ['{:d} '.format(int(z0[i])) + sents[i] for i in ind]

ind = np.argsort(dists[ind])
X_succ = [X_succ[i] for i in ind]

fname = 'out/{0}_{1}_{2:.2f}.txt'.format(DATA, ALGO, EPS)
with open(fname, 'w') as w:
    w.write('\n'.join(X_succ))

ind = np.where(np.logical_and(z0 == z1, z1 == z2))[0]
ind = np.random.choice(ind, size=50, replace=False)
X_fail = ['{:d} '.format(int(z0[i])) + sents[i] for i in ind]
ind = np.argsort(dists[ind])
X_fail = [X_fail[i] for i in ind]

fname = 'out/{0}_{1}_{2:.2f}_fail_50.txt'.format(DATA, ALGO, EPS)
with open(fname, 'w') as w:
    w.write('\n'.join(X_fail))

succ = np.logical_and(z0 == z1, z1 != z2).flatten()
tmp = np.vstack((lens, succ)).T
fname = 'out/{0}_{1}_len-succ.npy'.format(DATA, ALGO)
np.save(fname, tmp)
