from __future__ import print_function
import os
import glob
import os.path as osp
import argparse
import sys
import h5py
import time
import datetime
import numpy as np
from tabulate import tabulate

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.optim import lr_scheduler
from torch.distributions import Bernoulli

from utils import Logger, read_json, write_json, save_checkpoint
from models import *
from rewards import compute_reward

parser = argparse.ArgumentParser("Pytorch code for unsupervised video summarization with REINFORCE")

# Model options
parser.add_argument('--input-dim', type=int, default=1024, help="input dimension (default: 1024)")
parser.add_argument('--hidden-dim', type=int, default=256, help="hidden unit dimension of DSN (default: 256)")
parser.add_argument('--num-layers', type=int, default=1, help="number of RNN layers (default: 1)")
parser.add_argument('--rnn-cell', type=str, default='Bi-lstm', help="RNN cell type (default: lstm)")


# Optimization options
parser.add_argument('--lr', type=float, default=1e-02, help="learning rate (default: 1e-05)")
parser.add_argument('--weight-decay', type=float, default=1e-05, help="weight decay rate (default: 1e-05)")
parser.add_argument('--max-epoch', type=int, default=10, help="maximum epoch for training (default: 60)")
parser.add_argument('--stepsize', type=int, default=10, help="how many steps to decay learning rate (default: 30)")
parser.add_argument('--gamma', type=float, default=0.1, help="learning rate decay (default: 0.1)")
parser.add_argument('--num-episode', type=int, default=1, help="number of episodes (default: 5)")
parser.add_argument('--start_idx', type=int, default=0, help="number of episodes (default: 5)")
parser.add_argument('--number_of_picks', type=int, default=2, help="number of frames to select")
parser.add_argument('--path_to_features', type=str, default='path', help="path to weighted features")
parser.add_argument('--classes', type=int, default=19, help="number of classes in dataset")
parser.add_argument('--beta', type=float, default=0.01, help="weight for summary length penalty term (default: 0.01)")
# Misc
parser.add_argument('--seed', type=int, default=1, help="random seed (default: 1)")
parser.add_argument('--gpu', type=str, default='0', help="which gpu devices to use")
parser.add_argument('--use-cpu', action='store_true', help="use cpu device")
parser.add_argument('--evaluate', action='store_true', help="whether to do evaluation only")
parser.add_argument('--save-dir', type=str, default='log', help="path to save output (default: 'log/')")
parser.add_argument('--resume', type=str, default='', help="path to resume file")
parser.add_argument('--verbose', action='store_true', help="whether to show detailed test results")
parser.add_argument('--save-results', action='store_true', help="whether to save output results")

args = parser.parse_args()

torch.manual_seed(args.seed)
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
use_gpu = torch.cuda.is_available()
if args.use_cpu: use_gpu = False

def main():
    if not args.evaluate:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_train.txt'))
    else:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_test.txt'))
    print("==========\nArgs:{}\n==========".format(args))

    if use_gpu:
        print("Currently using GPU {}".format(args.gpu))
        cudnn.benchmark = True
        torch.cuda.manual_seed_all(args.seed)
    else:
        print("Currently using CPU")

    print("Initialize dataset")

    fpath = args.path_to_features+'*'
    features=[]
    fpaths=[]
    for f in sorted(glob.glob(fpath)):
        fpaths.append(f)
        f1=np.load(f,allow_pickle=True)
        features.append(f1)

    features_all=(np.stack(features))

    dist = features_all.shape[0]
    number_of_picks = args.number_of_picks

    start=args.start_idx
    
    features=features_all[start:start+dist,:]

    model = DSN(in_dim=args.classes, hid_dim=args.hidden_dim)
    

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    if args.stepsize > 0:
        scheduler = lr_scheduler.StepLR(optimizer, step_size=args.stepsize, gamma=args.gamma)

    if args.resume:
        print("Loading checkpoint from '{}'".format(args.resume))
        checkpoint = torch.load(args.resume)
        model.load_state_dict(checkpoint)
    else:
        start_epoch = 0

    if use_gpu:
        model = nn.DataParallel(model).cuda()


    print("==> Start training")
    start_time = time.time()
    model.train()
    baseline=0.
    best_reward=0.0
    best_pi=[]

    os.system('rm -r ./selection/')
    os.system('mkdir selection')

    for epoch in range(start_epoch, args.max_epoch):
        seq = features
        seq = torch.from_numpy(seq).unsqueeze(0) # input shape (1, seq_len, dim)
        if use_gpu: seq = seq.cuda()
        probs = model(seq) # output shape (1, seq_len, 1)
        cost = args.beta * (probs.mean() - 0.5)**2 
        m = Bernoulli(probs)
        epis_rewards = []
        for _ in range(args.num_episode):
            actions = m.sample()
            log_probs = m.log_prob(actions)
            reward,pick_idxs = compute_reward(seq, actions,probs,nc=args.classes, picks=number_of_picks, use_gpu=use_gpu)
            if(reward>best_reward):
                best_reward=reward
                best_pi=pick_idxs
            expected_reward = log_probs.mean() * (reward - baseline)
            cost -= expected_reward # minimize negative expected reward
            epis_rewards.append(reward.item())

        optimizer.zero_grad()
        cost.backward()
        optimizer.step()
        baseline = 0.9 * baseline + 0.1 * np.mean(epis_rewards) # update baseline reward via moving average
        print("epoch {}/{}\t reward {}\t".format(epoch+1, args.max_epoch, np.mean(epis_rewards)))
        f=open('selection/'+str(start)+'.txt','w')
        for idx in best_pi:
            f.write(fpaths[start+idx]+'\n')
        f.close()


    elapsed = round(time.time() - start_time)
    elapsed = str(datetime.timedelta(seconds=elapsed))
    print("Finished. Total elapsed time (h:m:s): {}".format(elapsed))

    model_state_dict = model.module.state_dict() if use_gpu else model.state_dict()
    model_save_path = osp.join(args.save_dir, 'model_epoch' + str(args.max_epoch) + '.pth.tar')
    save_checkpoint(model_state_dict, model_save_path)
    print("Model saved to {}".format(model_save_path))



if __name__ == '__main__':
    main()
