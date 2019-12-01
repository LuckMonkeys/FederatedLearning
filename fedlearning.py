import time
import torch.nn as nn
import argparse
import copy
import numpy as np
from torchvision import datasets, transforms
import torch
import random
from utils.sampling import mnist_iid, mnist_noniid, cifar_iid, mnist_remove
from utils.options import args_parser
from models.Nets import CNNMnist, CNNCifar
from models.test import test_img,test
from utils.sampling import set_user
import pickle
import torch.distributed as dist
from torch.utils import data
from random import Random
from torch.utils.data import  Dataset
from math import ceil
from torch.autograd import Variable
import torch.nn.functional as F
import torch.multiprocessing as mp


class Partition(object):
    """ Dataset-like object, but only access a subset of it. """

    def __init__(self, data, index):
       self.data = data
       # self.index = index
       self.index=list(index)

    def __len__(self):
        return len(self.index)

    def __getitem__(self, index):
        data_idx = self.index[index]
        return self.data[data_idx]

class DataPartitioner(object):
    """ Partitions a dataset into different chuncks. """

    def __init__(self, data, sizes=[0.7, 0.2, 0.1], seed=1234):
        self.data = data
        self.partitions = []
        rng = Random()
        rng.seed(seed)
        data_len = len(data)
        indexes = [x for x in range(0, data_len)]
        rng.shuffle(indexes)

        for frac in sizes:
            part_len = int(frac * data_len)
            self.partitions.append(indexes[0:part_len])
            indexes = indexes[part_len:]

    def use(self, partition):
        return DatasetSplit(self.data, self.partitions[partition])


class DatasetSplit(Dataset):
    def __init__(self, dataset, idxs):
        self.dataset = dataset
        self.idxs = list(idxs)

    def __len__(self):
        return len(self.idxs)

    def __getitem__(self, item):
        image, label = self.dataset[self.idxs[item]]
        return image, label


def partition_dataset():
    """ Partitioning dataset """

    trans_mnist = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
    dataset_train = datasets.MNIST('data/', train=True, download=True, transform=trans_mnist)
    size = dist.get_world_size()-1
    bsz = int(128 / float(size))
    num_users = size
    dict_users = mnist_iid(dataset_train, num_users)
    print("world-size:{}".format(dist.get_world_size()))
    print("rank:{}".format(dist.get_rank()))
    partition = Partition(dataset_train, dict_users[dist.get_rank()-1])
    train_set = torch.utils.data.DataLoader(
        partition, batch_size=bsz, shuffle=True)
    return train_set, bsz


def average_gradients(w,group,allgroup):
    """ Gradient averaging. """


    rank=dist.get_rank()
    world_size=dist.get_world_size()
    if rank==0:
        """ server """
        #聚合参数，用broadcast模拟rec
        send_buff=[]
        for i in range(1,world_size):
            dist.broadcast(tensor=w,src=i,group=group[i-1])
            temp=copy.deepcopy(torch.tensor(w))

            send_buff.append(temp)

        #整合平均
        w_avg=send_buff[0]
        for i in range(1, len(send_buff)):
            w_avg+= send_buff[i]
        w_avg = torch.div(w_avg, len(send_buff))

        #向所有client发放参数
        dist.broadcast(tensor=w_avg,src=0,group=allgroup)

        return w_avg
    else:
        """client"""
        #发送参数给server，用broadcast模拟send
        dist.broadcast(tensor=w,src=rank,group=group[rank-1])

        #从server接收参数
        w_avg=copy.deepcopy(w)
        dist.broadcast(tensor=w_avg,src=0,group=allgroup)

        return w_avg


def main_worker(gpu,ngpus_per_node, args):
    print("gpu:",gpu)
    args.gpu = gpu
    newrank=args.rank*ngpus_per_node+gpu
    ngpus_per_node * args.world_size
    #初始化,使用tcp方式进行通信
    print("begin init")
    dist.init_process_group(init_method=args.init_method,backend="nccl",world_size=args.world_size,rank=newrank)
    print("end init")

    #建立通信group,rank=0作为server，用broadcast模拟send和rec，需要server和每个client建立group
    group=[]
    for i in range(1,args.world_size):
        group.append(dist.new_group([0,i]))
    allgroup=dist.new_group([i for i in range(args.world_size)])


    if newrank==0:
        """ server"""

        print("使用{}号服务器的第{}块GPU作为server".format(args.rank,gpu))

    #在模型训练期间，server只负责整合参数并分发，不参与任何计算
        #设置cpu
        args.device = torch.device('cuda:{}'.format(args.gpu) if torch.cuda.is_available() and args.gpu != -1 else 'cpu')


        net=CNNMnist().to(args.device)
        w_avg=copy.deepcopy(net.state_dict())
        for j in range(args.epochs):
            if j==args.epochs-1:
                for i in w_avg.keys():
                    temp=w_avg[i].to(args.device)
                    w_avg[i]=average_gradients(temp,group,allgroup)
            else:
                for i in w_avg.keys():
                    temp=w_avg[i].to(args.device)
                    average_gradients(temp,group,allgroup)
        torch.save(w_avg,'w_wag')
        net. load_state_dict(w_avg)
        #加载测试数据
        trans_mnist = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
        dataset_test = datasets.MNIST('data/', train=False, download=True, transform=trans_mnist)
        test_set= torch.utils.data.DataLoader(dataset_test, batch_size=args.bs)
        test_accuracy, test_loss = test(net, test_set, args)
        print("Testing accuracy: {:.2f}".format(test_accuracy))
        print("Testing loss: {:.2f}".format(test_loss))

    else:
        """clents"""

        print("使用{}号服务器的第{}块GPU作为第{}个client".format(args.rank, gpu,newrank))

        #设置gpu
        args.device = torch.device('cuda:{}'.format(args.gpu) if torch.cuda.is_available() and args.gpu != -1 else 'cpu')




        #加载测试数据
        trans_mnist = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
        dataset_test = datasets.MNIST('data/', train=False, download=True, transform=trans_mnist)
        test_set= torch.utils.data.DataLoader(dataset_test, batch_size=args.bs)




        print("begin train...")
        net = CNNMnist().to(args.device)
        print(net)

        train_set, bsz = partition_dataset()
        optimizer = torch.optim.SGD(net.parameters(), lr=args.lr, momentum=0.5)
        num_batches = ceil(len(train_set.dataset) / float(bsz))
        start=time.time()
        for epoch in range(args.epochs):
            for iter in range(3):
                epoch_loss = 0.0
                for data, target in train_set:
                    data,target=data.to(args.device),target.to(args.device)
                    data, target = Variable(data), Variable(target)
                    optimizer.zero_grad()
                    output = net(data)
                    loss = F.nll_loss(output, target)
                    epoch_loss += loss.item()
                    loss.backward()
                    optimizer.step()
                if iter ==3-1:
                    print('Rank ',dist.get_rank(), ', epoch ', epoch, ': ',epoch_loss / num_batches)

            """federated learning"""
            w_avg=copy.deepcopy(net.state_dict())

            for k in w_avg.keys():
                print("k:",k)
                temp=average_gradients(w_avg[k].to(args.device),group,allgroup)
                w_avg[k]=temp
            net.load_state_dict(w_avg)


        end=time.time()
        print(" training time:{}".format((end-start)))


        train_accuracy, train_loss = test(net, train_set, args)
        print("Training accuracy: {:.2f}".format(train_accuracy))
        print("Training loss: {:.2f}".format(train_loss))


if __name__ == '__main__':

    args = args_parser()

    ngpus_per_node = torch.cuda.device_count()
    args.world_size = ngpus_per_node * args.world_size
    mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
