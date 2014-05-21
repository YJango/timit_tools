import cPickle
import os
import sys
import time
import socket

import numpy
from collections import OrderedDict, defaultdict

import theano
import theano.tensor as T
from theano.tensor.shared_randomstreams import RandomStreams
from theano import shared

from logistic_timit import LogisticRegression 
from prep_timit import load_data

#DATASET = '/home/gsynnaeve/datasets/TIMIT'
#DATASET = '/media/bigdata/TIMIT'
#DATASET = '/fhgfs/bootphon/scratch/gsynnaeve/TIMIT/wo_sa'
DATASET = '/fhgfs/bootphon/scratch/gsynnaeve/TIMIT/train_dev_test_split'
if socket.gethostname() == "syhws-MacBook-Pro.local":
    DATASET = '/Users/gabrielsynnaeve/postdoc/datasets/TIMIT_train_dev_test'
N_FEATURES = 40  # filterbanks
N_FRAMES = 13  # HAS TO BE AN ODD NUMBER 
               #(same number before and after center frame)
MIN_FRAMES_PER_SENTENCE = 26
BORROW = True
output_file_name = 'SRNN'


class DatasetSentencesIterator(object):
    """ An iterator on sentences of the dataset. """

    def __init__(self, x, y, phn_to_st, nframes=1):
        self._x = x
        self._y = y
        self._start_end = [[0]]
        self._nframes = nframes
        self._memoized_x = defaultdict(lambda: {})
        i = 0
        for i, s in enumerate(y == phn_to_st['!ENTER[2]']):
            if s and i - self._start_end[-1][0] > MIN_FRAMES_PER_SENTENCE:
                self._start_end[-1].append(i)
                self._start_end.append([i])
#            elif s:
#                print "less than", MIN_FRAMES_PER_SENTENCE, "frames in",
#                print self._start_end[-1][0], i
        self._start_end[-1].append(i+1)

    def _stackpad(self, start, end):
        """ Method because of the memoization. """
        if start in self._memoized_x and end in self._memoized_x[start]:
            return self._memoized_x[start][end]
        x = self._x[start:end]
        nf = self._nframes
        ret = numpy.zeros((x.shape[0], x.shape[1] * nf), dtype='float32')
        ba = (nf - 1) / 2  # before/after
        for i in xrange(x.shape[0]):
            ret[i] = numpy.pad(x[max(0, i - ba):i + ba +1].flatten(),
                    (max(0, (ba -i) * x.shape[1]),
                        max(0, ((i + ba + 1) - x.shape[0]) * x.shape[1])),
                    'constant', constant_values=(0, 0))
        self._memoized_x[start][end] = ret
        return ret

    def __iter__(self):
        for start, end in self._start_end:
            #yield shared(self._x[start:end], borrow=BORROW), shared(self._y[start:end], borrow=BORROW)
            if self._nframes > 1:
                #yield shared(self._stackpad(start, end)), shared(self._y[start:end])
                yield self._stackpad(start, end), self._y[start:end]
            else:
                yield self._x[start:end], self._y[start:end]


class ReLU(object):
    def __init__(self, rng, input, n_in, n_out, W=None, b=None):
        if W is None:
            W_values = numpy.asarray(rng.uniform(
                    low=-numpy.sqrt(6. / (n_in + n_out)),
                    high=numpy.sqrt(6. / (n_in + n_out)),
                    size=(n_in, n_out)), dtype=theano.config.floatX)
            W_values *= 4  # TODO CHECK
            W = theano.shared(value=W_values, name='W', borrow=True)
        if b is None:
            b_values = numpy.zeros((n_out,), dtype=theano.config.floatX)
            b = theano.shared(value=b_values, name='b', borrow=True)
        self.input = input
        self.W = W
        self.b = b
        lin_output = T.dot(input, self.W) + self.b
        self.output = lin_output + abs(lin_output) / 2.
        self.params = [self.W, self.b]


class StackReLU(ReLU):
    def __init__(self, rng, input, in_stack, n_in, n_in_stack, n_out,
            W=None, Ws=None, b=None):
        super(StackReLU, self).__init__(rng, input, n_in, n_out)
        if Ws is None:
            Ws_values = numpy.asarray(rng.uniform(
                    low=-numpy.sqrt(6. / (n_in + n_out)),
                    high=numpy.sqrt(6. / (n_in + n_out)),
                    size=(n_in, n_out)), dtype=theano.config.floatX)
            Ws_values *= 4  # TODO CHECK
            Ws = shared(value=Ws_values, name='Ws', borrow=True)
        self.Ws = Ws  # weights of the reccurrent connection
        self.params = [self.W, self.b, self.Ws]  # order is important! W, b, Ws
        lin_output = T.dot(input, self.W) + T.dot(in_stack, self.Ws) + self.b
        self.output = lin_output + abs(lin_output) / 2.


class SRNN(object):
    """Stacking ReLU Neural Network
    """

    def __init__(self, numpy_rng, theano_rng=None, 
            n_ins=N_FEATURES * N_FRAMES,
            relu_layers_sizes=[1024, 1024, 1024],
            recurrent_connections=[2],  # layer(s)
            n_outs=62 * 3,
            rho=0.90, eps=1.E-6):
        """ TODO 
        """

        self.relu_layers = []
        self.params = []
        self.n_layers = len(relu_layers_sizes)
        self._rho = rho  # ``momentum'' for adadelta
        self._eps = eps  # epsilon for adadelta
        self._accugrads = []  # for adadelta
        self._accudeltas = []  # for adadelta
        self.n_outs = n_outs

        assert self.n_layers > 0

        if not theano_rng:
            theano_rng = RandomStreams(numpy_rng.randint(2 ** 30))

        self.x = T.fmatrix('x')
        self.y = T.ivector('y')
        self.previous_p_y_given_x = T.fmatrix('p_y_given_x')

        input_repu_layer = StackReLU(rng=numpy_rng,
                input=self.x, in_stack=self.previous_p_y_given_x,
                n_in=n_ins, n_in_stack=n_outs, n_out=relu_layers_sizes[0])
        self.relu_layers.append(input_repu_layer)
        self.params.extend(input_repu_layer.params)
        self._accugrads.extend([shared(value=numpy.zeros((n_ins, relu_layers_sizes[0]), dtype='float32'), name='accugrad_W', borrow=True), shared(value=numpy.zeros((relu_layers_sizes[0], ), dtype='float32'), name='accugrad_b', borrow=True)])
        self._accugrads.extend([shared(value=numpy.zeros((n_ins, relu_layers_sizes[0]), dtype='float32'), name='accugrad_Ws', borrow=True)])
        self._accudeltas.extend([shared(value=numpy.zeros((n_outs, relu_layers_sizes[0]), dtype='float32'), name='accudelta_W', borrow=True), shared(value=numpy.zeros((relu_layers_sizes[0], ), dtype='float32'), name='accudelta_b', borrow=True)])
        self._accudeltas.extend([shared(value=numpy.zeros((n_outs, relu_layers_sizes[0]), dtype='float32'), name='accudelta_Ws', borrow=True)])

        for i in xrange(1, self.n_layers):
            input_size = relu_layers_sizes[i-1]
            layer_input = self.relu_layers[-1].output

            relu_layer = ReLU(rng=numpy_rng,
                    input=layer_input,
                    n_in=input_size,
                    n_out=relu_layers_sizes[i])

            self.relu_layers.append(relu_layer)

            self.params.extend(relu_layer.params)
            self._accugrads.extend([shared(value=numpy.zeros((input_size, relu_layers_sizes[i]), dtype='float32'), name='accugrad_W', borrow=True), shared(value=numpy.zeros((relu_layers_sizes[i], ), dtype='float32'), name='accugrad_b', borrow=True)])
            self._accudeltas.extend([shared(value=numpy.zeros((input_size, relu_layers_sizes[i]), dtype='float32'), name='accudelta_W', borrow=True), shared(value=numpy.zeros((relu_layers_sizes[i], ), dtype='float32'), name='accudelta_b', borrow=True)])


        # We now need to add a logistic layer on top of the MLP
        self.logLayer = LogisticRegression(
            input=self.relu_layers[-1].output,
            n_in=relu_layers_sizes[-1],
            n_out=n_outs)
        self.params.extend(self.logLayer.params)
        self._accugrads.extend([shared(value=numpy.zeros((relu_layers_sizes[-1], n_outs), dtype='float32'), name='accugrad_W', borrow=True), shared(value=numpy.zeros((n_outs, ), dtype='float32'), name='accugrad_b', borrow=True)]) # TODO
        self._accudeltas.extend([shared(value=numpy.zeros((relu_layers_sizes[-1], n_outs), dtype='float32'), name='accudelta_W', borrow=True), shared(value=numpy.zeros((n_outs, ), dtype='float32'), name='accudelta_b', borrow=True)]) # TODO

        # compute the cost for second phase of training, defined as the
        # negative log likelihood of the logistic regression (output) layer
        self.finetune_cost = self.logLayer.negative_log_likelihood(self.y)
        self.finetune_cost_sum = self.logLayer.negative_log_likelihood_sum(self.y)
        self.p_y_given_x = self.logLayer.p_y_given_x

        # compute the gradients with respect to the model parameters
        # symbolic variable that points to the number of errors made on the
        # minibatch given by self.x and self.y
        self.errors = self.logLayer.errors(self.y)

    def get_SGD_trainer(self):
        """ Returns a plain SGD minibatch trainer with learning rate as param.
        FIXME TODO
        """
        # TODO
        return -1

    def get_adadelta_trainer(self):
        """ Returns an Adadelta (Zeiler 2012) trainer using self._rho and self._eps params.
        """
        cost = self.finetune_cost_sum
        # compute the gradients with respect to the model parameters
        gparams = T.grad(cost, self.params)

        # compute list of fine-tuning updates
        updates = OrderedDict()
        for accugrad, accudelta, param, gparam in zip(self._accugrads,
                self._accudeltas, self.params, gparams):
            # c.f. Algorithm 1 in the Adadelta paper (Zeiler 2012)
            agrad = self._rho * accugrad + (1 - self._rho) * gparam * gparam
            dx = - T.sqrt((accudelta + self._eps) / (agrad + self._eps)) * gparam
            updates[accudelta] = self._rho * accudelta + (1 - self._rho) * dx * dx
            updates[param] = param + dx
            updates[accugrad] = agrad
            
        #p_y_given_x_init = shared(numpy.asarray(numpy.random.uniform((1, self.n_outs)), dtype='float32'))
        p_y_given_x_init = T.zeros((1, self.n_outs)) + 1./self.n_outs
        #def step(x, previous_p_y_given_x=p_y_given_x_init):

        def one_step(x_t, previous_p_y_given_x):
            self.x = x_t
            self.previous_p_y_given_x = previous_p_y_given_x
            return [x_t, self.p_y_given_x]
        
        batch_x = T.fmatrix('batch_x')
        batch_y = T.ivector('batch_y')
        #batch_previous_p_y_given_x = T.fmatrix('batch_previous_p_y_given_x')
        [x, p_y_given_x], _ = theano.scan(lambda x_t, p_y_g_x_m1, *_: one_step(x_t, p_y_g_x_m1),
                sequences=batch_x[:-1],
                outputs_info=[None, p_y_given_x_init],)
                

        train_fn = theano.function(inputs=[theano.Param(batch_x), 
            theano.Param(batch_y)],
            outputs=cost,
            updates=updates,
            givens={self.y: batch_y,
                self.previous_p_y_given_x: T.concatenate([p_y_given_x_init, 
                    p_y_given_x], axis=0),
                self.x: batch_x
                })

        return train_fn

    def get_adagrad_trainer(self):
        """ Returns an Adagrad (Duchi et al. 2010) trainer using a learning rate.
        FIXME TODO
        """
        # TODO
        return -1

    def score_classif(self, given_set):
        """ Returns functions to get current classification scores. """
        batch_x = T.fmatrix('batch_x')
        batch_y = T.ivector('batch_y')
        score = theano.function(inputs=[theano.Param(batch_x), theano.Param(batch_y)],
                outputs=self.errors,
                givens={self.x: batch_x, self.y: batch_y})

        # Create a function that scans the entire set given as input
        def scoref():
            return [score(batch_x, batch_y) for batch_x, batch_y in given_set]

        return scoref


def test_SRNN(finetune_lr=0.01, pretraining_epochs=0,
             pretrain_lr=0.01, k=1, training_epochs=200, # TODO 100+
             dataset=DATASET, batch_size=100):
    """

    :type learning_rate: float
    :param learning_rate: learning rate used in the finetune stage
    :type pretraining_epochs: int
    :param pretraining_epochs: number of epoch to do pretraining
    :type pretrain_lr: float
    :param pretrain_lr: learning rate to be used during pre-training
    :type k: int
    :param k: number of Gibbs steps in CD/PCD
    :type training_epochs: int
    :param training_epochs: maximal number of iterations ot run the optimizer
    :type dataset: string
    :param dataset: path the the pickled dataset
    :type batch_size: int
    :param batch_size: the size of a minibatch
    """

    print "loading dataset from", dataset
    #datasets = load_data(dataset, nframes=N_FRAMES, features='fbank', scaling='normalize', cv_frac=0.2, speakers=False, numpy_array_only=True) 
    #datasets = load_data(dataset, nframes=N_FRAMES, features='fbank', scaling='student', cv_frac='fixed', speakers=False, numpy_array_only=True) 
    datasets = load_data(dataset, nframes=1, features='fbank', scaling='student', cv_frac='fixed', speakers=False, numpy_array_only=True) 
    #datasets = load_data(dataset, nframes=1, features='fbank', scaling='student', cv_frac=0.2, speakers=False, numpy_array_only=True) 

    train_set_x, train_set_y = datasets[0]  # if speakers, do test/test/test
    valid_set_x, valid_set_y = datasets[1]
    test_set_x, test_set_y = datasets[2]

    print "dataset loaded!"
    print "train set size", train_set_x.shape[0]
    print "validation set size", valid_set_x.shape[0]
    print "test set size", test_set_x.shape[0]
    print "phones in train", len(set(train_set_y))
    print "phones in valid", len(set(valid_set_y))
    print "phones in test", len(set(test_set_y))

    to_int = {}
    with open('timit_to_int_and_to_state_dicts_tuple.pickle') as f:  # TODO
        to_int, _ = cPickle.load(f)
    train_set_iterator = DatasetSentencesIterator(train_set_x, train_set_y,
            to_int, N_FRAMES)
    valid_set_iterator = DatasetSentencesIterator(valid_set_x, valid_set_y,
            to_int, N_FRAMES)
    test_set_iterator = DatasetSentencesIterator(test_set_x, test_set_y,
            to_int, N_FRAMES)

    # numpy random generator
    numpy_rng = numpy.random.RandomState(123)
    print '... building the model'

    dbn = SRNN(numpy_rng=numpy_rng, n_ins=N_FRAMES * N_FEATURES,
              relu_layers_sizes=[1024, 1024, 1024],
              n_outs=len(set(train_set_y)))

    # get the training, validation and testing function for the model
    print '... getting the finetuning functions'
    train_fn = dbn.get_adadelta_trainer()
    train_scoref = dbn.score_classif(train_set_iterator)
    valid_scoref = dbn.score_classif(valid_set_iterator)
    test_scoref = dbn.score_classif(test_set_iterator)

    print '... finetuning the model'
    # early-stopping parameters
    patience = 1000  # look as this many examples regardless TODO
    patience_increase = 2.  # wait this much longer when a new best is
                            # found
    improvement_threshold = 0.995  # a relative improvement of this much is
                                   # considered significant

    best_validation_loss = numpy.inf
    test_score = 0.
    start_time = time.clock()

    done_looping = False
    epoch = 0

    while (epoch < training_epochs) and (not done_looping):
        epoch = epoch + 1
        avg_costs = []
        for iteration, (x, y) in enumerate(train_set_iterator):
            avg_cost = train_fn(x, y)
            avg_costs.append(avg_cost)
            #print('  epoch %i, sentence %i, '
            #'avg cost for this sentence %f' % \
            #      (epoch, iteration, avg_cost))
        print('  epoch %i, avg costs %f' % \
              (epoch, numpy.mean(avg_costs)))
        print('  epoch %i, training error %f %%' % \
              (epoch, numpy.mean(train_scoref()) * 100.))

        # we check the validation loss on every epoch
        validation_losses = valid_scoref()
        this_validation_loss = numpy.mean(validation_losses)  # TODO this is a mean of means (with different lengths)
        print('  epoch %i, validation error %f %%' % \
              (epoch, this_validation_loss * 100.))
        # if we got the best validation score until now
        if this_validation_loss < best_validation_loss:
            with open(output_file_name + '.pickle', 'w') as f:
                cPickle.dump(dbn, f)
            # improve patience if loss improvement is good enough
            if (this_validation_loss < best_validation_loss *
                improvement_threshold):
                patience = max(patience, iteration * patience_increase)
            # save best validation score and iteration number
            best_validation_loss = this_validation_loss
            # test it on the test set
            test_losses = test_scoref()
            test_score = numpy.mean(test_losses)  # TODO this is a mean of means (with different lengths)
            print(('  epoch %i, test error of '
                   'best model %f %%') %
                  (epoch, test_score * 100.))
        if patience <= iteration:  # TODO correct that
            done_looping = True
            break

    end_time = time.clock()
    print(('Optimization complete with best validation score of %f %%, '
           'with test performance %f %%') %
                 (best_validation_loss * 100., test_score * 100.))
    print >> sys.stderr, ('The fine tuning code for file ' +
                          os.path.split(__file__)[1] +
                          ' ran for %.2fm' % ((end_time - start_time)
                                              / 60.))
    with open(output_file_name + '.pickle', 'w') as f:
        cPickle.dump(dbn, f)


if __name__ == '__main__':
    test_SRNN()
    # TODO args