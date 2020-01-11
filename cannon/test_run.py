#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys, time
import numpy as np
#import matplotlib.pyplot as pl
import logging

# child side
from jades_patch import JadesPatch
from forcepho.proposal import Proposer
from forcepho.model import GPUPosterior

# parent side
from dispatcher import SuperScene

logging.basicConfig(level=logging.DEBUG)


def dump_to_h5(filename, patch, active, fixed,
               pixeldatadict={}):
    pix = ["xpix", "ypix", "ierr"]
    meta = ["D", "CW", "crpix", "crval", "exposure_start"]
    extra = ["bandlist", "epaths"]
    with h5py.File(filename) as out:

        for a in pix:
            arr = getattr(patch, a)
            pdat = np.split(arr, np.cumsum(patch.exposure_N)[:-1])
            out.create_dataset(a, data=np.array(pdat))

        for a in meta + extra:
            arr = getattr(patch, a)
            out.create_dataset(a, data=np.array(arr))

        for a, arr in pixeldatadict.keys():
            out.create_dataset(a, data=arr)

        out.create_dataset("active", data=active)
        out.create_dataset("fixed", data=active)


if __name__ == "__main__":

    from config import config
    logger = logging.getLogger(__name__)

    # Build ingredients (parent and child sides)
    sceneDB = SuperScene(config.initial_catalog,
                         maxactive_per_patch=config.maxactive_per_patch)
    logger.info("Made SceneDB")
    patcher = JadesPatch(metastore=config.metastorefile,
                         psfstore=config.psfstorefile,
                         pixelstore=config.pixelstorefile,
                         splinedata=config.splinedatafile)
    logger.info("Made patch")

    # checkout region (parent operation)
    # seed_index = 444  # good source to build a scene from
    region, active, fixed = sceneDB.checkout_region()
    logger.info("checked out scene with {} active sources".format(len(active)))
    sr, sid, ra, dec = region.radius*3600, active[0]["source_index"], region.ra, region.dec
    logger.info("scene of radius {:3.2f} arcsec centered on source {} at (ra, dec)=({}, {})".format(sr, sid, ra, dec))

    # Build patch on CPU side (child operation)
    # Note this is the *fixed* source metadata
    patcher.build_patch(region, fixed, allbands=config.bandlist)
    logger.info("built patch with {} fixed sources".format(len(fixed)))
    logger.info("Patch has {} pixels".format(len(patcher.data)))
    original = np.split(patcher.data, np.cumsum(patcher.exposure_N)[:-1])
    parfixed = patcher.scene.get_all_source_params().copy()
    pfixed = patcher.scene.get_proposal()
    logger.info("got fixed proposal vector")
    print(pfixed)

    # Send patch to GPU (with fixed sources)
    patcher.return_residual = True
    logger.info("Sending to gpu....")
    gpu_patch = patcher.send_to_gpu()
    logger.info("Initial Patch sent")

    # Evaluate (and subtract) fixed sources
    logger.info("Making proposer and sending fixed proposal")
    proposer = Proposer(patcher)
    out = proposer.evaluate_proposal(pfixed)
    fixed_residual = out[-1]
    logger.info("Fixed sources subtracted")

    #print("Pointers before swap:")
    # check the cuda pointers
    #for k, v in patcher.cuda_ptrs.items():
    #    print(k, int(v))

    # Build active patch
    logger.info("Replacing cpu metadata with active sources")
    patcher.pack_meta(active)
    #print(patcher.scene)
    paractive = patcher.scene.get_all_source_params().copy()
    pactive = patcher.scene.get_proposal()
    logger.info("got active proposal vector")

    #sys.exit()

    logger.info("Swapping fixed/active metadata and residual/data on GPU")
    patcher.swap_on_gpu()

    #print("Pointers after swap:")
    # check the cuda pointers
    #for k, v in patcher.cuda_ptrs.items():
    #    print(k, int(v))

    # send proposal to GPU
    #patcher.return_residual = False
    logger.info("Making new proposer and sending active proposal")
    proposer = Proposer(patcher)
    out = proposer.evaluate_proposal(pactive)

    extra = {"data": original,
             "fixed_residual": np.array(fixed_residual),
             "active_residual": np.array(out[-1]),
             "active_chi2": out[0],
             "active_grad": out[1]
             }

    fn = "patch{}_ra{:6.4f}_dec{:6.4f}.h5".format("test", region.ra, region.dec)
    dump_to_h5(fn, proposer.patch, active, fixed,
               pixeldatadict=extra)
    logger.info("wrote patch data to {}".format(fn))

    logger.info("Timing proposal evaluation")
    proposer.patch.return_residual = False
    tstart = time.time()
    ntime = 50
    for i in range(ntime):
        o = proposer.evaluate_proposal(pactive)
    dur = time.time() - tstart
    logger.info("Completed {} proposals in {}s".format(ntime, dur))

    logger.info("Done")