
# coding: utf-8

# ## 20Mn data MovieLens Experiment

# An experiment on 20mn MovieLens Dataset.
# 
# The core idea is to use rnn to process sequence log data, to replace trained user embedding on crossfiltering nn model.
# 
# * This notebook is to test transfer learning

# In[1]:


import pandas as pd
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torch.optim import Adam


# In[2]:


from ray.lprint import lprint
from ray.matchbox_lego import AttLSTM
l = lprint("experiment with RNN+CF on movielens 20m data")


# In[3]:


CUDA = torch.cuda.is_available()
SEQ_LEN = 19 # sequence length
DIM = 100 # hidden vector lenth, embedding length
l.p("has GPU cuda",CUDA)


# In[4]:


# %ls /data/ml-20m


# In[5]:


DATA = "/data/ml-20m/ratings.csv"


# In[6]:


l.p("loading csv file", DATA)
rate_df = pd.read_csv(DATA)
l.p("csv file loaded")


# In[7]:


len(rate_df)


# In[8]:


rate_df.groupby("userId").count()["movieId"].min()
# The minimum number of movies a user have rated


# In[9]:


userId = list(set(rate_df["userId"]))
movieId = list(set(rate_df["movieId"]))
print("total number of users and movies:\t",len(userId),"\t",len(movieId))


# In[10]:


l.p("making dictionary")
u2i = dict((v,k) for k,v in enumerate(userId))
m2i = dict((v,k) for k,v in enumerate(movieId))
i2u = dict((k,v) for k,v in enumerate(userId))
i2m = dict((k,v) for k,v in enumerate(movieId))


# In[11]:


# Translating original index to the new index
l.p("translating the index")
rate_df["movieIdx"] = rate_df.movieId.apply(lambda x:m2i[x]).astype(int)
rate_df["userIdx"] = rate_df.userId.apply(lambda x:u2i[x]).astype(int)
l.p("normalize the rating")
rate_df["rating"] = rate_df["rating"]/5


# ### Train /Valid Split: K fold Validation 

# In[12]:


l.p("generating groubby slice")
def get_user_trail(rate_df):
    return rate_df.sort_values(by=["userId","timestamp"]).groupby("userId")
    #gb.apply(lambda x:x.sample(n = 20, replace = False))
gb = get_user_trail(rate_df)


# In[13]:


from torch.utils.data.dataset import Dataset
from torch.utils.data.dataloader import DataLoader
import math


# In[14]:


KEEP_CONSEQ = True

if KEEP_CONSEQ:
    # keep the consequtivity among the items the user has rated
    def sample_split(x):
        sample_idx = math.floor(np.random.rand()*(len(x) - SEQ_LEN - 1))
        seq_and_y = x[sample_idx:sample_idx + SEQ_LEN+1]
        return seq_and_y
else:
    # randomly pick the right amount of sample from user's record
    pick_k = np.array([0]*SEQ_LEN +[1])==1

    def sample_split(x):
        sampled = x.sample(n = 20, replace = False)
        seq = sampled.head(19).sort_values(by="timestamp")
        y = sampled[pick_k]
        return pd.concat([seq,y])
    
from ray.matchbox import DF_Dataset

def read_x(df):
    return df[["userIdx","movieIdx"]].values

def read_y(df):
    return df[["rating"]].values

class rnn_record(Dataset):
    def __init__(self, gb):
        """
        A pytorch dataset object designed to group logs into user behavior sequence
        """
        self.gb = gb
        self.make_seq()
    
    def make_seq(self):
        """
        Resample the data
        """
        self.all_seq = self.gb.apply(sample_split)
        
    def __len__(self):
        return len(self.gb)
        
    def __getitem__(self,idx):
        """
        next(generator) will spit out the 'return' of this function
        this is a single row in the batch
        """
        df = self.all_seq.loc[idx]
        seq = df.head(SEQ_LEN)[["movieIdx","rating"]].values
        targ = df.head(SEQ_LEN+1).tail(1)[["movieIdx","rating"]].values
        targ_v, targ_y =targ[:,0], targ[:,1]
        return idx,seq,targ_v,targ_y


# In[15]:


# Testing data generator

# data_gb = get_user_trail(rate_df)
# rr = rnn_record(data_gb)
# rr.all_seq

# dl = DataLoader(rr,shuffle=True,batch_size=1)
# gen = iter(dl)
# next(gen)


# In[28]:


### Model Transfer
class cf(nn.Module):
    def __init__(self, hidden_size,u_size,v_size):
        """
        Deep embedded cross filtering neural network
        """
        super(cf,self).__init__()
        self.hidden_size = hidden_size
        self.u_size = u_size
        self.v_size = v_size
        self.emb_u = nn.Embedding(u_size,hidden_size)
        self.emb = nn.Embedding(v_size,hidden_size)
        
        self.mlp = nn.Sequential(*[
            nn.Dropout(.3),
            nn.Linear(hidden_size*2, 256, bias=False),
            nn.BatchNorm1d(256),
            nn.Linear(256,1,bias=False),
            nn.Sigmoid(),
        ])
    def forward(self,u,v):
        x = torch.cat([self.emb_u(u).squeeze(1),self.emb(v).squeeze(1)],dim=1)
        x = self.mlp(x)
        return x
        
### Model

class mLinkNet(nn.Module):
    def __init__(self, hidden_size,v_size):
        """
        mLinkNet, short for missing link net
        """
        super(mLinkNet,self).__init__()
        self.hidden_size = hidden_size
        self.v_size = v_size
        self.emb = nn.Embedding(v_size,hidden_size)
        
        self.rnn = AttLSTM(mask_activation = "sigmoid",
                        input_size = self.hidden_size+1,# AttLSTM input dim = DIM +1, left 1 element for rating
                          hidden_size= hidden_size+1, 
                          num_layers=2,
                          batch_first = True,
                          dropout=0)
        
        self.mlp = nn.Sequential(*[
            nn.Dropout(.3),
            nn.Linear(hidden_size*2+1, 256, bias=False),
            nn.BatchNorm1d(256),
            nn.Linear(256,1,bias=False),
            nn.Sigmoid(),
        ])
    
    def forward(self,seq,targ_v):
        seq_vec = torch.cat([self.emb(seq[:,0].long()),
                             seq[:,1].unsqueeze(-1).float()], dim=2)
        output, (hn,cn),mask = self.rnn(seq_vec)
        x = torch.cat([output,self.emb(targ_v.long()).squeeze(1)],dim=1)
        return self.mlp(x)


# In[29]:


def tran_action(*args,**kwargs):
    x,y = args[0]
    if CUDA:
        x,y = x.cuda(),y.cuda()
        x,y = x.squeeze(0),y.squeeze(0)
    u,v = x[:,:1],x[:,1:]
    opt_.zero_grad()
    y_ = m0(u,v)
    y = y.float()
    loss = loss_func(y_,y)
    loss.backward()
    mae = torch.mean(torch.abs(y_-y))
    opt_.step()
    return {"loss":loss.item(),"mae":mae.item()}

def action(*args,**kwargs):
    # get data from data feeder
    idx,seq,targ_v,y = args[0]
    if CUDA:
        seq,targ_v,y = seq.cuda(),targ_v.cuda(),y.cuda()
    y = y.float()
    
    # Clear the Jacobian Matrix
    opt.zero_grad()
    
    # Predict y hat
    y_ = mln(seq, targ_v)
    # Calculate Loss
    loss = loss_func(y_,y)
    
    # Backward Propagation
    loss.backward()
    opt.step()
    # Mean Absolute Loss as print out metrics
    mae = torch.mean(torch.abs(y_-y))
    if kwargs["ite"] == train_len - 1: # resample the sequence
        trainer.train_data.dataset.make_seq()
    return {"loss":loss.item(),"mae":mae.item()}

def val_action(*args,**kwargs):
    """
    A validation step
    Exactly the same like train step, but no learning, only forward pass
    """
    idx,seq,targ_v,y = args[0]
    if CUDA:
        seq,targ_v,y = seq.cuda(),targ_v.cuda(),y.cuda()
    y = y.float()
    
    y_ = mln(seq, targ_v)
    
    loss = loss_func(y_,y)
    mae = torch.mean(torch.abs(y_-y))
    if kwargs["ite"] == valid_len - 1:
        trainer.val_data.dataset.make_seq()
    return {"loss":loss.item(),"mae":mae.item()}


# In[ ]:


l.p("making train/test split")
user_count = len(userId)
K = 2
valid_split = dict({})
random = np.random.rand(user_count)
from ray.matchbox import Trainer

def shift(modelfrom, modelto):
    """
    switch gpu mem allocation and transfer embedding weights
    """
    if CUDA:
        modelfrom.cpu()
    modelto.emb.weight.data = modelfrom.emb.weight.data
    if CUDA:
        modelto.cuda()
    

l.p("start training")
for fold in range(K):
    valid_split = ((fold/K) < random)*(random <= ((fold+1)/K))
    train_idx = np.array(range(user_count))[~valid_split]
    valid_idx = np.array(range(user_count))[valid_split]
    
    trans_df = rate_df[rate_df.userId.isin(train_idx)]
    train_df = rate_df[rate_df.userId.isin(train_idx)]
    valid_df = rate_df[rate_df.userId.isin(valid_idx)]
    
    tran_ds = DF_Dataset(trans_df,read_x,read_y,512,shuffle=True)
    
    # Since user id mapping doesn't matter any more.
    # It's easier to make a dataset with contineous user_id.
    train_u2i = dict((v,k) for k,v in enumerate(set(train_df.userId)))
    valid_u2i = dict((v,k) for k,v in enumerate(set(valid_df.userId)))
    train_df["userId"] = train_df.userId.apply(lambda x:train_u2i[x])
    valid_df["userId"] = valid_df.userId.apply(lambda x:valid_u2i[x])
    
    train_gb = get_user_trail(train_df)
    valid_gb = get_user_trail(valid_df)
    
    l.p("generating dataset","train")
    train_ds = rnn_record(train_gb)
    l.p("generating dataset","valid")
    valid_ds = rnn_record(valid_gb)
    l.p("dataset generated")

    l.p("creating model")
    m0 = cf(hidden_size = DIM, 
                u_size = len(userId), 
                v_size = len(movieId))
    
    mln = mLinkNet(hidden_size = DIM, 
               v_size = len(movieId))

    opt_ = Adam(m0.parameters())
    opt = Adam(mln.parameters())
    loss_func = nn.MSELoss()
    
    pretrain = Trainer(tran_ds,batch_size=1,print_on=2)
    trainer = Trainer(train_ds, val_dataset=valid_ds, batch_size=16, print_on=3)
    
    train_len = len(trainer.train_data)
    valid_len = len(trainer.val_data)
    l.p("train_len",train_len)
    l.p("valid_len",valid_len)
    
    pretrain.action = tran_action
    trainer.action  = action
    trainer.val_action  = val_action
    
    l.p("fold training start", fold)
    for ep in range(12):
        shift(mln,m0)
        pretrain.train(1,name="attlstm_cf_fold_%s_pretrain_ep_%s"%(fold,ep)) # transfer learning first
        shift(m0,mln)
        trainer.train(1,name="attlstm_cf_fold_%s_ep_%s"%(fold,ep))
    l.p("fold training finished",fold)
l.p("training finished")


# In[ ]:


torch.save(mln.state_dict(),"/data/rnn_cf_0.0.5.transfer_learning.npy")

