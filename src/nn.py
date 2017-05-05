import torch
import torch.cuda as cuda
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
from pytorch_rnn import *
import utils
import numpy as np
# import tensorflow as tf

class EncoderRNN(nn.Module):
    def __init__(self, lang, hidden_size, max_length, emb_dims):
        super(EncoderRNN, self).__init__()
        self.hidden_size = hidden_size
        self.max_length = max_length
        self.emb_dims = emb_dims
        self.embedding = nn.Embedding(lang.n_words, emb_dims).cuda()
        self.rnn = LSTM(emb_dims, hidden_size, batch_first=True).cuda()
        
    def forward(self, input, hidden):
        embedded = self.embedding(input)
        embedded = embedded.view(input.size()[0], self.max_length, -1)
        output = embedded
        output, hidden = self.rnn(output, hidden)
        return output, hidden

    def initHidden(self, batch_size):
        return (Variable(cuda.FloatTensor(1, batch_size, self.hidden_size).zero_()),
               Variable(cuda.FloatTensor(1, batch_size, self.hidden_size).zero_()))


class DecoderRNN(nn.Module):
    def __init__(self, lang, hidden_size, context_size, persona_size, emb_dims, max_length, embedding):
        super(DecoderRNN, self).__init__()
        self.emb_dims = emb_dims
        self.hidden_size = hidden_size
        self.context_size = context_size
        self.max_length = max_length
        self.embedding = embedding
        self.lang = lang
        if persona_size:
            self.rnn = LSTM(emb_dims + context_size + persona_size, hidden_size, batch_first=True).cuda()
        else:
            self.rnn = LSTM(emb_dims + context_size, hidden_size, batch_first=True).cuda()
        self.out = nn.Linear(hidden_size, lang.n_words).cuda()
        self.softmax = nn.LogSoftmax()
        
    def forward(self, input, hidden, context, p1, p2):

        # context = N x H need to convert N x (T + 1) x H
        # input = N x (T + 1)
        # hidden = decoder_hidden

        N, T = input.size()
        T -= 1 # as input is T+1
        H = context.size()[1]

        output = self.embedding(input)

        multi_context = [context for t in xrange(T + 1)]

        multi_context = torch.cat(multi_context, 1).view(N, T+1, H)
        # output is N x (T + 1) x D, need to concatenate with context which is N x H to produce N x T x (D + H)
        items = [output, multi_context]
        if p1 is not None and p2 is not None:
            # Only speaker embedding for now
            items.append(p2.view(1, -1))

        output = torch.cat(items, 2).view(N, T+1, -1)
        output = F.relu(output)
        output, hidden = self.rnn(output, hidden)
        output = output.contiguous().view(-1, H)
        output = self.softmax(self.out(output)).view(N, T+1, self.lang.n_words)
        return output, hidden

    def initHidden(self, batch_size):
        return (Variable(cuda.FloatTensor(1, batch_size, self.hidden_size).zero_()),
               Variable(cuda.FloatTensor(1, batch_size, self.hidden_size).zero_()))

class AttentionDecoder(nn.Module):
    def __init__(self, lang, max_length, hidden_size, context_size, persona_size, D_size, emb_dims, embedding):
        super(AttentionDecoder, self).__init__()
        self.emb_dims = emb_dims
        self.max_length = max_length
        self.hidden_size = hidden_size
        self.embedding = embedding
        self.lang = lang
        self.context_size = context_size
        if persona_size:
            self.rnn = LSTM(emb_dims + context_size + persona_size, hidden_size, batch_first=True).cuda()
        else:
            self.rnn = LSTM(emb_dims + context_size, hidden_size, batch_first=True).cuda()
        self.out = nn.Linear(hidden_size, lang.n_words).cuda()
        self.softmax = nn.LogSoftmax()
        self.D_size = D_size
        self.a_layer = torch.nn.Softmax()
        self.r_layer = torch.nn.Linear(hidden_size, D_size).cuda() # here second context size is D
        self.u_layer = torch.nn.Linear(D_size, 1).cuda() # here context size is D
        
    def forward(self, input, hidden, encoder_states, wf_mat, mask, p1, p2):
        
        N = input.size()[0]
        T = encoder_states.size()[1]

        output = self.embedding(input).view(N, -1)

        r_t = self.r_layer(hidden[0][0]) # D x 1 get the hidden state of the first element in the batch
        # print "r", r_t.size()
        # print "w_f", wf_mat.size()

        r_copy_t = [r_t.view(N, 1, self.D_size) for t in xrange(T)]
        r_copy_t = torch.cat(r_copy_t, 1)

        tanh = F.tanh(wf_mat + r_copy_t) # N x T x D
        # NT x D
        # pass to u layer
        # reshape to N x T x 1
        tanh = tanh.contiguous().view(-1, self.D_size)

        # print "tanh", tanh.size()
        u_t = self.u_layer(tanh).view(N, T, 1) # f x 1
        u_t = torch.addcmul(Variable(cuda.FloatTensor(N, T).zero_()),u_t, mask).view(N, T)

        # print "u", u_t.size()
        a_t = self.a_layer(u_t) # N x T

        # reshape encoder_states from N x T x H to NT x H
        encoder_states = encoder_states.contiguous().view(N*T, self.context_size)
        # reshape a_t to NTx1
        a_t = a_t.contiguous().view(N*T, 1)

        a_t = [a_t for h in xrange(self.context_size)]
        a_t = torch.cat(a_t, 1)

        # weighted product
        context = torch.addcmul(Variable(cuda.FloatTensor(N*T, self.context_size).zero_()), a_t, encoder_states)
        context = context.view(N, T, self.context_size) # this is NT x H , reshape to N x T x H
        # get weighted sum along the time axis
        context = torch.sum(context, 1).view(N, self.context_size) # context size should be N x H

        items = [output, context]
        if p1 is not None and p2 is not None:
            # Only speaker embedding for now
            items.append(p2.view(1, -1))

        output = torch.cat(items, 1)
        output = F.relu(output).view(N, 1, -1)

        output, hidden = self.rnn(output, hidden)  # output will be N x T x H

        # reshape output to N x H, convert to softmax to get N x V, reshape to N x 1 x V
        output = output.contiguous().view(N, self.hidden_size)
        output = self.softmax(self.out(output)).view(N, 1, self.lang.n_words)

        return output, hidden

    def initHidden(self, batch_size):
        return (Variable(cuda.FloatTensor(1, batch_size, self.hidden_size).zero_()),
               Variable(cuda.FloatTensor(1, batch_size, self.hidden_size).zero_()))

class Seq2Seq(object):
    # Does not work on batches yet, just works on a single question and answer

    def __init__(self, lang, enc_size, dec_size, emb_dims, max_length, learning_rate, attention=False, reload_model=False, persona=False, persona_size=None):

        self.attention = attention
        self.persona = persona
        if persona is True:
            self.persona_embedding = nn.Embedding(lang.n_persona, persona_size).cuda() # emb_dims of character is 20
        if reload_model is True:
            self.encoder = torch.load(open('../models/encoder.pth'))
            self.decoder = torch.load(open('../models/decoder.pth'))        
        else:
            self.encoder = EncoderRNN(lang, enc_size, max_length, emb_dims)
            persona_size = None
            if attention is True:
                self.D_size = self.encoder.hidden_size
                self.decoder = AttentionDecoder(lang, max_length, dec_size, enc_size, persona_size, self.D_size, emb_dims, self.encoder.embedding)
            else:
                self.decoder = DecoderRNN(lang, dec_size, enc_size, persona_size, emb_dims, max_length, self.encoder.embedding)

        self.max_length = max_length
        self.encoder_optimizer = optim.Adam(self.encoder.parameters(), lr=learning_rate)
        self.decoder_optimizer = optim.Adam(self.decoder.parameters(), lr=learning_rate)
        self.lang = lang
        if attention is True:
            self.wf_layer = torch.nn.Linear(self.encoder.hidden_size, self.D_size).cuda()
        
        self.criterion = nn.NLLLoss() # Negative log loss
        # self.summary_op = tf.summary.merge_all()

    def save_model(self):

        torch.save(self.encoder, '../models/encoder.pth')
        torch.save(self.decoder, '../models/decoder.pth')

    def forward(self, batch_pairs, train=True):

        N = len(batch_pairs)

        # pair = tuple of (question, answer)
        # if self.persona is True:
        #     (persona1, input_variable, input_length, persona2, target_variable, target_length) = utils.variablesFromPairPersona(self.lang, pair)
        #     p1 = self.persona_embedding(persona1).view(1, -1)
        #     p2 = self.persona_embedding(persona2).view(1, -1)
        # else:
        encoder_input_batch = Variable(cuda.LongTensor(N, self.max_length).zero_(), requires_grad=False)
        decoder_input_batch = Variable(cuda.LongTensor(N, self.max_length + 1).zero_(), requires_grad=False) # start with SOS token
        decoder_target_batch = Variable(cuda.LongTensor(N, self.max_length + 1).zero_(), requires_grad=False)
        encoder_input_batch_len = []
        decoder_input_batch_len = []
        for i in xrange(N):    
            (encoder_input_variable, encoder_sequence_length, decoder_input_variable, decoder_target_variable, decoder_sequence_length) = utils.variablesFromPair(self.lang, batch_pairs[i])
            encoder_input_batch[i] = encoder_input_variable
            decoder_input_batch[i] = decoder_input_variable
            decoder_target_batch[i] = decoder_target_variable
            encoder_input_batch_len.append(encoder_sequence_length)
            decoder_input_batch_len.append(decoder_sequence_length)
        encoder_input_batch_len = cuda.LongTensor(encoder_input_batch_len)
        decoder_input_batch_len = cuda.LongTensor(decoder_input_batch_len)
        p1 = None
        p2 = None

        encoder_hidden = self.encoder.initHidden(N)
        decoder_hidden = self.decoder.initHidden(N)

        self.encoder_optimizer.zero_grad()
        self.decoder_optimizer.zero_grad()
        
        # Encode the sentence
        encoder_output, encoder_hidden = self.encoder(encoder_input_batch, encoder_hidden)

        if self.attention is False:
            last_encoder_states = Variable(cuda.FloatTensor(N, self.encoder.hidden_size).zero_())
            for i in xrange(N):
                last_encoder_states[i] = encoder_output[i, encoder_input_batch_len[i], :]

        else:
            F = encoder_output.contiguous().view(-1, self.encoder.hidden_size) # is NT x E
            self.wf = self.wf_layer(F).view(N, self.max_length, self.D_size) # output is NT x D => N x T x D
            mask = Variable(cuda.FloatTensor(N, self.max_length, 1).zero_(), requires_grad=False)
            for i in xrange(N):
                t = encoder_input_batch_len[i]
                mask[i, :t+1, :] = 1

        # print torch.mean(encoder_output)
        del encoder_input_batch
        
        response = []
        loss = 0
        if train is True:
            if self.attention is False:
                decoder_output_batch, decoder_hidden = self.decoder(decoder_input_batch, decoder_hidden, last_encoder_states, p1, p2)
            else:
                decoder_step_input = torch.t(Variable(cuda.LongTensor([[utils.SOS_token]*N]), requires_grad=False))
                decoder_output_batch = []
                for t in xrange(self.max_length):
                    decoder_step_output, decoder_hidden = self.decoder(decoder_step_input, decoder_hidden, 
                                                                        encoder_output, self.wf, mask, p1, p2)
                    decoder_output_batch.append(decoder_step_output)
                    #input, hidden, encoder_states, wf_mat, p1, p2
                decoder_output_batch = torch.cat(decoder_output_batch, 1)

            for i in xrange(N):
                t = decoder_input_batch_len[i]
                loss += self.criterion(decoder_output_batch[i, :t+1, :], decoder_target_batch[i, :t+1])
        else:
            # greedy decode
            response = [[self.lang.index2word[utils.SOS_token]] for i in xrange(N)]
            decoder_step_input = torch.t(Variable(cuda.LongTensor([[utils.SOS_token]*N]), requires_grad=False)) # To make it N x 1
            for t in xrange(self.max_length):
                if self.attention is True:
                    decoder_step_output, decoder_hidden = self.decoder(decoder_step_input, decoder_hidden, 
                                                                        encoder_output, self.wf, mask, p1, p2)
                else:
                    decoder_step_output, decoder_hidden = self.decoder(decoder_step_input, decoder_hidden, last_encoder_states, p1, p2)
                decoder_step_output = decoder_step_output.view(N, self.lang.n_words)
                scores, idx = torch.max(decoder_step_output, 1)
                decoder_step_input = idx
                for i in xrange(N):
                    word = self.lang.index2word[idx[i].data[0]]
                    # print word, np.exp(scores[i].data[0])
                    if response[i][-1] != self.lang.index2word[utils.EOS_token]:
                        response[i].append(word)

            # assert False
            # response = []
            # for di in xrange(self.max_length):
            #     if self.attention is True:
            #         decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden, encoder_states, self.wf, p1, p2)
            #     else:
            #         decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden, encoder_output[0][0], p1, p2)
            #     topv, topi = decoder_output.data.topk(1)
            #     ind = topi[0][0]
            #     if ind == utils.EOS_token:
            #         break
            #     decoder_input = Variable(cuda.LongTensor([[ind]]), requires_grad=False)
            #     response.append(self.lang.index2word[ind])

            # This implementation of beam search is wrong, we need to predict and follow the pointers back.
            # beam_size = 5
            # di = 0
            # while di < self.max_length:


                    
                
        # tf.summary.scalar('loss', loss)

        
        # Step back
        if train is True:
            loss.backward()
            self.encoder_optimizer.step()
            self.decoder_optimizer.step()
        
        del decoder_target_batch
        del decoder_input_batch
        # del decoder_output_batch
        response = [' '.join(resp[1:-1]) for resp in response]
        return response, loss