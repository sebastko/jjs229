from __future__ import print_function
import argparse
import os
import random
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
import numpy as np

from bitmap import generate_inf_cases


parser = argparse.ArgumentParser()
parser.add_argument('--dataset', default='gen', help='gen | kaggle')
parser.add_argument('--dataroot', required=False, help='path to dataset')
parser.add_argument('--workers', type=int, help='number of data loading workers', default=2)
parser.add_argument('--batchSize', type=int, default=64, help='input batch size')
parser.add_argument('--imageSize', type=int, default=64, help='the height / width of the input image to network')
parser.add_argument('--nz', type=int, default=100, help='size of the latent z vector')
parser.add_argument('--ngf', type=int, default=64)
parser.add_argument('--ndf', type=int, default=64)
parser.add_argument('--niter', type=int, default=25, help='number of epochs to train for')
parser.add_argument('--lr', type=float, default=0.0002, help='learning rate, default=0.0002')
parser.add_argument('--beta1', type=float, default=0.5, help='beta1 for adam. default=0.5')
parser.add_argument('--cuda', action='store_true', help='enables cuda')
parser.add_argument('--dry-run', action='store_true', help='check a single training cycle works')
parser.add_argument('--ngpu', type=int, default=1, help='number of GPUs to use')
parser.add_argument('--netG', default='', help="path to netG (to continue training)")
parser.add_argument('--netD', default='', help="path to netD (to continue training)")
parser.add_argument('--outf', default='.', help='folder to output images and model checkpoints')
parser.add_argument('--manualSeed', type=int, help='manual seed')
parser.add_argument('--classes', default='bedroom', help='comma separated list of classes for the lsun data set')

opt = parser.parse_args()
print(opt)

try:
    os.makedirs(opt.outf)
except OSError:
    pass

if opt.manualSeed is None:
    opt.manualSeed = random.randint(1, 10000)
print("Random Seed: ", opt.manualSeed)
random.seed(opt.manualSeed)
torch.manual_seed(opt.manualSeed)

cudnn.benchmark = True


if torch.cuda.is_available() and not opt.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")


shuffle=True
if opt.dataset == 'gen':
    class DataGenerator(torch.utils.data.IterableDataset):
        def __init__(self, base_seed):
            super(DataGenerator).__init__()
            self.base_seed = base_seed

        def __iter__(self):
            worker_info = torch.utils.data.get_worker_info()
            if worker_info is None:  # single-process data loading, return the full iterator
                seed = self.base_seed
            else:  # in a worker process
                # split workload
                worker_id = worker_info.id
                seed = self.base_seed + worker_id
            for delta, prev, stop in generate_inf_cases(True, seed, return_one_but_last=True):
                yield (
                    np.array(np.reshape(prev, (1, 25, 25)), dtype=np.float32),
                    np.array(np.reshape(stop, (1, 25, 25)), dtype=np.float32)
                )

    dataset = DataGenerator(823131)
    shuffle=False
    nc=1
elif opt.dataset == 'kaggle':
    # TODO: load kaggle .csv dataset
    if opt.dataroot is None:
        raise ValueError("`dataroot` parameter is required for dataset \"%s\"" % opt.dataset)

    nc=1
    pass
elif opt.dataset == 'mnist':
    if opt.dataroot is None:
        raise ValueError("`dataroot` parameter is required for dataset \"%s\"" % opt.dataset)
    dataset = dset.MNIST(root=opt.dataroot, download=True,
                       transform=transforms.Compose([
                           transforms.Resize(opt.imageSize),
                           transforms.ToTensor(),
                           transforms.Normalize((0.5,), (0.5,)),
                       ]))
    nc=1

elif opt.dataset == 'fake':
    dataset = dset.FakeData(image_size=(3, opt.imageSize, opt.imageSize),
                            transform=transforms.ToTensor())
    nc=3

assert dataset
dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batchSize,
                                         shuffle=shuffle, num_workers=int(opt.workers))

device = torch.device("cuda:0" if opt.cuda else "cpu")
ngpu = int(opt.ngpu)
nz = int(opt.nz)
ngf = int(opt.ngf)
ndf = int(opt.ndf)


# custom weights initialization called on netG and netD
def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        torch.nn.init.normal_(m.weight, 0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        torch.nn.init.normal_(m.weight, 1.0, 0.02)
        torch.nn.init.zeros_(m.bias)


# torch.nn.ConvTranspose2d(
#   in_channels: int,
#   out_channels: int,
#   kernel_size: Union[T, Tuple[T, T]],
#   stride: Union[T, Tuple[T, T]] = 1,
#   padding: Union[T, Tuple[T, T]] = 0,
#   output_padding: Union[T, Tuple[T, T]] = 0,
#   groups: int = 1,
#   bias: bool = True,
#   dilation: int = 1,
#   padding_mode: str = 'zeros')
class Generator(nn.Module):
    def __init__(self, ngpu):
        super(Generator, self).__init__()
        self.ngpu = ngpu

        self.understand_stop = nn.Sequential(
            # input is (nc) x 25 x 25
            nn.Conv2d(1, ndf, 5, 2, 2, bias=False, padding_mode='circular'),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf) x 13 x 13
            nn.Conv2d(ndf, ndf * 2, 5, 2, 2, bias=False, padding_mode='circular'),
            nn.BatchNorm2d(ndf * 2),
            nn.LeakyReLU(0.2, inplace=True)
            # state size. (ndf*2) x 7 x 7
        )
        """
        nn.Conv2d(ndf * 2, ndf * 4, 5, 2, 2, bias=False, padding_mode='circular'),
        nn.BatchNorm2d(ndf * 4),
        nn.LeakyReLU(0.2, inplace=True),
        # state size. (ndf*4) x 4 x 4
        nn.Conv2d(ndf * 4, 1, 4, 1, 0, bias=False)
        """

        self.z_gen = nn.Sequential(
            # input is Z, going into a convolution
            nn.ConvTranspose2d(     nz, ngf * 4, 4, 1, 0, bias=False),
            nn.BatchNorm2d(ngf * 4),
            nn.ReLU(True),
            # state size. (ngf*4) x 4 x 4
            nn.ConvTranspose2d(ngf * 4, ngf * 2, 5, 2, 2, bias=False), # padding_mode='circular' not available in ConvTranspose2d
            nn.BatchNorm2d(ngf * 2),
            nn.ReLU(True)
            # state size. (ngf*2) x 7 x 7
        )
        """
        nn.ConvTranspose2d(ngf * 2,     ngf, 5, 2, 2, bias=False),
        nn.BatchNorm2d(ngf),
        nn.ReLU(True),
        # state size. (ngf) x 13 x 13
        nn.ConvTranspose2d(    ngf,      nc, 5, 2, 2, bias=False),
        nn.Tanh()
        #nn.Sigmoid()
        # state size. (nc) x 25 x 25
        """

        self.final_gen = nn.Sequential(
            # state size. (ndf*2 + ngf*2) x 7 x 7
            nn.ConvTranspose2d(ndf*2 + ngf * 2,     ngf, 5, 2, 2, bias=False),
            nn.BatchNorm2d(ngf),
            nn.ReLU(True),
            # state size. (ngf) x 13 x 13
            nn.ConvTranspose2d(    ngf,      nc, 5, 2, 2, bias=False),
            nn.Tanh()
            #nn.Sigmoid()
            # state size. (nc) x 25 x 25
        )


    def forward(self, stop, z):
        # TODO: fix CUDA:
        #if input.is_cuda and self.ngpu > 1:
        #    output = nn.parallel.data_parallel(self.main, input, range(self.ngpu))
        #else:

        # Concatenate channels from stop understanding and z_gen:
        stop_emb = self.understand_stop(stop)
        z_emb = self.z_gen(z)
        emb = torch.cat([stop_emb, z_emb], dim=1)

        output = self.final_gen(emb)
        return output


netG = Generator(ngpu).to(device)
netG.apply(weights_init)
if opt.netG != '':
    netG.load_state_dict(torch.load(opt.netG))
print(netG)

# torch.nn.Conv2d(
#   in_channels: int,
#   out_channels: int,
#   kernel_size: Union[T, Tuple[T, T]],
#   stride: Union[T, Tuple[T, T]] = 1,
#   padding: Union[T, Tuple[T, T]] = 0,
#   dilation: Union[T, Tuple[T, T]] = 1,
#   groups: int = 1,
#   bias: bool = True,
#   padding_mode: str = 'zeros')
class Discriminator(nn.Module):
    def __init__(self, ngpu):
        super(Discriminator, self).__init__()
        self.ngpu = ngpu
        self.main = nn.Sequential(
            # input is (nc) x 25 x 25
            nn.Conv2d(2, ndf, 5, 2, 2, bias=False, padding_mode='circular'),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf) x 13 x 13
            nn.Conv2d(ndf, ndf * 2, 5, 2, 2, bias=False, padding_mode='circular'),
            nn.BatchNorm2d(ndf * 2),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*2) x 7 x 7
            nn.Conv2d(ndf * 2, ndf * 4, 5, 2, 2, bias=False, padding_mode='circular'),
            nn.BatchNorm2d(ndf * 4),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*4) x 4 x 4
            nn.Conv2d(ndf * 4, 1, 4, 1, 0, bias=False),
            nn.Sigmoid()
        )

    def forward(self, start, stop):
        # start and stop are separate channels now.
        input = torch.cat([start, stop], dim=1)
        if input.is_cuda and self.ngpu > 1:
            output = nn.parallel.data_parallel(self.main, input, range(self.ngpu))
        else:
            output = self.main(input)

        return output.view(-1, 1).squeeze(1)


netD = Discriminator(ngpu).to(device)
netD.apply(weights_init)
if opt.netD != '':
    netD.load_state_dict(torch.load(opt.netD))
print(netD)

criterion = nn.BCELoss()

fixed_noise = torch.randn(opt.batchSize, nz, 1, 1, device=device)
real_label = 1
fake_label = 0

# setup optimizer
optimizerD = optim.Adam(netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
optimizerG = optim.Adam(netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))

if opt.dry_run:
    opt.niter = 1

#for epoch in range(opt.niter):
epoch = 0
for i, data in enumerate(dataloader, 0):
    ############################
    # (1) Update D network: maximize log(D(x)) + log(1 - D(G(z)))
    ###########################
    # train with real
    netD.zero_grad()
    start_real_cpu = data[0].to(device)
    stop_real_cpu = data[1].to(device)
    batch_size = start_real_cpu.size(0)
    label = torch.full((batch_size,), real_label,
                       dtype=start_real_cpu.dtype, device=device)

    output = netD(start_real_cpu, stop_real_cpu)
    errD_real = criterion(output, label)
    errD_real.backward()
    D_x = output.mean().item()

    # train with fake
    noise = torch.randn(batch_size, nz, 1, 1, device=device)
    fake = netG(stop_real_cpu, noise)
    label.fill_(fake_label)
    output = netD(fake.detach(), stop_real_cpu)
    errD_fake = criterion(output, label)
    errD_fake.backward()
    D_G_z1 = output.mean().item()
    errD = errD_real + errD_fake
    optimizerD.step()

    ############################
    # (2) Update G network: maximize log(D(G(z)))
    ###########################
    netG.zero_grad()
    label.fill_(real_label)  # fake labels are real for generator cost
    output = netD(fake, stop_real_cpu)
    errG = criterion(output, label)
    errG.backward()
    D_G_z2 = output.mean().item()
    optimizerG.step()

    print('[%d/%d][%d] Loss_D: %.4f Loss_G: %.4f D(x): %.4f D(G(z)): %.4f / %.4f'
          % (epoch, opt.niter, i,
             errD.item(), errG.item(), D_x, D_G_z1, D_G_z2))
    if i % 100 == 0:
        vutils.save_image(start_real_cpu,
                '%s/real_samples.png' % opt.outf,
                normalize=True)
        fake = netG(stop_real_cpu, fixed_noise) #.round()
        vutils.save_image(fake.detach(),
                '%s/fake_samples_epoch_%03d.png' % (opt.outf, epoch),
                normalize=True)
        # do checkpointing
        torch.save(netG.state_dict(), '%s/netG_epoch_%d.pth' % (opt.outf, epoch))
        torch.save(netD.state_dict(), '%s/netD_epoch_%d.pth' % (opt.outf, epoch))
        epoch += 1

    if opt.dry_run:
        break
