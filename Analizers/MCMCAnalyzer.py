#
# This is the MCMC module.
# it spits out chains that are compatible with CosmoMC
# it calculates cov matrix during burn-in.
# chain_num tells it to spit out multi-node chains.
# optional temperature makes it sample at a higher temperature but note that
# this guy, as opposed to cosmomc, reweights the weights on the fly.
#

import scipy.integrate as integrate
import scipy.linalg as la
import os.path as path
import scipy as sp
import copy
import random
import sys


class MCMCAnalyzer:
    def __init__(self, like, outfile, skip=5000, nsamp=100000, temp=1.0,
                 cov=None, chain_num=None, derived=False):

        self.like      = like
        self.outfile   = outfile
        self.nsamp     = nsamp
        self.skip      = skip
        self.temp      = float(temp)  # temperature
        self.chain_num = chain_num
        self.cpars     = like.freeParameters()
        self.N         = len(self.cpars)
        self.derived   = derived == 'True'

        minvals, maxvals = [], []
        for lb, hb in [p.bounds for p in self.cpars]:
            minvals.append(lb)
            maxvals.append(hb)
        self.minvals = sp.array(minvals)
        self.maxvals = sp.array(maxvals)
        print("Bounds:", self.minvals, self.maxvals)

        if (like.name() == "Composite"):
            self.sublikenames = like.compositeNames()
            self.composite = True
        else:
            self.composite = False

        if (cov == None):
            # make initial cov matrix from diagonal "errors"
            errs = [0.01*p.error**2 for p in self.cpars]
            self.init_pcov(sp.diag(errs))
        else:
            self.init_pcov(cov)

        if self.derived: self.AD = AllDerived()
        self.RunChain()


    def init_pcov(self, mat):
        self.chol = la.cholesky(mat)



    def RunChain(self):
        self.openFiles()
        self.cloglike, self.cloglikes = self. getLikes()
        # set up logofs based on the first log like which should be
        # the same for all chains. Better than nothing.
        # self.logofs=self.cloglike
        # Actually, above doesn't seem to work very well.
        # Instead, use zero, as our likelihoods never became very large
        self.logofs = 0
        # current weight
        self.cw     = 0
        # current counter
        self.co     = 0
        # mean for burin
        self.swx    = 0
        self.meanx  = sp.zeros(self.N)
        self.meanxx = sp.zeros((self.N, self.N))
        # max loglike
        self.maxloglike = -1e30
        # are we done
        self.done = False
        print("Starting chain...")

        while not (self.done):
            ppars, numout = self.GetProposal()
            self.cw += numout  ## things hitting outside the prior are formally rejected samples
            self.like.updateParams(ppars)
            ploglike, ploglikes = self.getLikes()
            if (sp.isnan(ploglike)):
                print("Something bad has happened, nan in loglike, assuming zero log")
                ploglike = -1e50
            # print cloglike, ploglike, [p.value for p in like.freeParameters()], [p.value for p in self.cpars]
            if (ploglike > self.cloglike):
                accept = True
            else:
                accept = (sp.exp((ploglike-self.cloglike)/self.temp)
                          > random.uniform(0., 1.))

            # print [p.value for p in ppars], accept, ploglike
            # stop
            if (accept):
                self.ProcessAccepted(ppars, ploglike, ploglikes)
            else:
                self.cw += 1 
        self.closeFiles()


    def openFiles(self):
        outfile = self.outfile
        if self.chain_num in [None, 1]:
            fpar = open(outfile + ".paramnames", 'w')
            for p in self.cpars:
                fpar.write(p.name + "\t\t\t" + p.Ltxname + "\n")

            if self.derived:
                for pd in self.AD.list:
                    fpar.write(pd.name + "\t\t\t" + pd.Ltxname + "\n")

            if self.composite:
                for name in self.sublikenames:
                    fpar.write(name + "_like \t\t\t" + name + "\n")
                fpar.write("theory_prior \t\t\t None \n")
            fpar.close()

        formstr = '%g ' + '%g '*(self.N+1)
        if self.derived:
            formstr += '%g '*(len(self.AD.list))

        if (self.composite):
            formstr += '%g '*(len(self.sublikenames)+1)
        formstr += '\n'

        if (self.chain_num == None):
            cfname  = outfile + ".txt"
            mlfname = outfile + ".maxlike"
        else:
            cfname  = outfile + "_%i.txt" % (self.chain_num)
            mlfname = outfile + "_%i.maxlike" % (self.chain_num)

        if (path.isfile(cfname)):
            print("Due to bad habits in the past, won't open existing file.", cfname)
            sys.exit(1)
        self.fout    = open(cfname, 'w')
        self.mlfout  = open(mlfname, 'w')
        self.formstr = formstr



    def closeFiles(self):
        self.fout.close()
        self.mlfout.close()


    def getLikes(self):
        if (self.composite):
            cloglikes = self.like.compositeLogLikes_wprior()
            cloglike  = cloglikes.sum()
        else:
            cloglikes = []
            cloglike  = self.like.loglike_wprior()
        return cloglike, cloglikes


    def GetProposal(self):
        vec = sp.zeros(self.N)
        numreject = 0
        while True:
            ppars = copy.deepcopy(self.cpars)
            step  = self.draw_pcov()
            #print ('step #', [p.value for p in  ppars])
            for i, p in enumerate(ppars):
                p.value += step[i]
                vec[i]   = p.value

            if all(vec > self.minvals) and all(vec < self.maxvals):
                return ppars, numreject
            numreject += 1
            


    def draw_pcov(self):
        a = sp.array([random.gauss(0., 1,) for _ in range(self.N)])
        return sp.dot(a, self.chol)



    def ProcessAccepted(self, ppars, ploglike, ploglikes):
        self.co += 1
        if (self.co % 100 == 0): #JAV 1000
            print("Accepted samples", self.co, self.cw)
        vec = [p.value for p in self.cpars]

        if (self.co > self.skip):
            # weight rescaled
            wers = self.cw*sp.exp((self.cloglike-self.logofs)
                               * (self.temp-1.0)/self.temp)

            tmp = [wers, -self.cloglike] + vec
            if self.derived:
                tmp += [pd.value for pd in self.AD.listDerived(self.like)]

            if (self.composite):
                outstr = self.formstr % tuple(tmp + self.cloglikes.tolist())
            else:
                outstr = self.formstr % tuple(tmp)

            self.fout.write(outstr)
            # Flush file on regular basis
            if (self.co % 100 == 0): #JAV 1000
                self.fout.flush()

            if (self.cloglike > self.maxloglike):
                self.maxloglike = self.cloglike
                print("New maxloglike", self.maxloglike)
                self.mlfout.seek(0)
                self.mlfout.write(outstr)
                self.mlfout.flush()

            if self.co > self.nsamp:
                self.done = True

        elif (self.co < self.skip):
            self.swx += self.cw
            v = sp.array(vec)
            self.meanx  += v*self.cw
            self.meanxx += sp.outer(v, v)*self.cw
            if (self.cw > 30):
                print("Still burning in, weight too large")
                self.chol *= 0.9
                print(self.cw)
        else:  # co==skip
            self.meanx  /= self.swx
            self.meanxx /= self.swx
            self.meanxx -= sp.outer(self.meanx, self.meanx)
            print("Re-initializing covariance matrix after burn-in")
            print(self.meanxx)
            for i, p in enumerate(self.cpars):
                print(p.name, p.value, sp.sqrt(self.meanxx[i, i]))

            self.init_pcov(self.meanxx)

        self.cw    = 1
        self.cpars = ppars
        self.cloglike = ploglike
        if self.composite:
            self.cloglikes = ploglikes




###---------------------------------------###


class AllDerived:
    def __init__(self):
        #self.cpars = cpars
        self.Ol   = Derivedparam('Ol', 0, '\Omega_\Lambda*')
        self.H0   = Derivedparam('H0', 0, 'H_0*')
        self.Age  = Derivedparam('Age', 0, 'Age[Gyr]*')
        self.list = [self.Ol, self.H0, self.Age]



    def listDerived(self, like):
        self.like  = like
        self.cpars = like.freeParameters()
        self.Ol.setValue(self.computeDerived('Ol'))
        self.H0.setValue(self.computeDerived('H0'))
        self.Age.setValue(self.computeDerived('Age'))
        return self.list


    def computeDerived(self, parname):
        if parname == 'Ol':
            for par in self.cpars:
                if par.name == 'Om':
                    return 1- par.value
        elif parname == 'H0':
            for par in self.cpars:
                if par.name == 'h':
                    return par.value*100
        elif parname == 'Age':
            return integrate.quad(self.compuAge, 0, 10**5)[0]/3.24076E-20/(3.154E7*1.0E9)
        else:
            sys.exit('Define derived parameter', parname)


    def compuAge(self, z):
        return 1.0/((1+z)*100.0*self.like.theory_.h*sp.sqrt(self.like.theory_.RHSquared_a(1.0/(1+z))))



class Derivedparam:
    def __init__(self, name, value, Ltxname=None):
        self.name = name
        if Ltxname:
            self.Ltxname = Ltxname
        else:
            self.Ltxname = name
        self.value = value


    def setLatexName(self, Ltx):
        self.Ltxname = Ltx

    def setValue(self, val):
        self.value = val




