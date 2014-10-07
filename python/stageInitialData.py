import sys
import re
import md5
import numpy as np
import time
import lsst.afw.image as afwImage
import lsst.afw.math as afwMath
import lsst.afw.geom as afwGeom
import lsst.daf.persistence as dafPersist
import lsst.daf.base as dafBase
from lsst.obs.sdss import SdssMapper as Mapper
from lsst.obs.sdss import convertfpM

doWriteSql = True
doc = """Generate an initial set of test-data for KB-MOD"""

def getRaDecl(wcs, x, y):
    ra, decl = wcs.pixelToSky(x, y)
    # Make ra go from -180 to +180 for ease of use
    if ra > np.pi * afwGeom.radians:
        ra = ra - 2 * np.pi * afwGeom.radians
    return ra.asDegrees(), decl.asDegrees()
    
def doit(args):
    dataId = args
    print "# Running", dataId
 
    # I need to create a separate instance in each thread
    mapper = Mapper(root = "/lsst7/stripe82/dr7/runs/", calibRoot = None, outputRoot = None)
    butler = dafPersist.ButlerFactory(mapper = mapper).create()

    # Grab science pixels
    im     = butler.get(datasetType="fpC", dataId = dataId).convertF()

    # Remove the 128 pixel duplicate overlap between fields
    # See python/lsst/obs/sdss/processCcdSdss.py for guidance
    bbox    = im.getBBox()
    begin   = bbox.getBegin()
    extent  = bbox.getDimensions()
    extent -= afwGeom.Extent2I(0, 128)
    tbbox   = afwGeom.BoxI(begin, extent)
    im      = afwImage.ImageF(im, tbbox, True)

    # Remove 1000 count pedestal
    im    -= 1000.0 

    # Create image variance from gain
    calib, gain = butler.get(datasetType="tsField", dataId = dataId)
    var    = afwImage.ImageF(im, True)
    var   /= gain

    # Note I need to do a bit extra for the mask; I actually need to call
    # convertfpM with allPlanes = True to get all the SDSS info
    #
    # mask   = butler.get(datasetType="fpM", dataId = dataId)
    fpMFile = butler.mapper.map_fpM(dataId = dataId).getLocations()[0]
    mask    = convertfpM(fpMFile, True)
    # Remove the 128 pixel duplicate overlap...
    mask    = afwImage.MaskU(mask, tbbox, True)

    # Subtract off background, and scale by stdev
    # This will turn the image into "sigma"
    if False:
        ctrl      = afwMath.StatisticsControl(5.0, 5)
        bitPlanes = mask.getMaskPlaneDict().values()
        bitMask   = 2**bitPlanes[0]
        for plane in bitPlanes[1:]:
            bitMask |= 2**bitPlanes[plane]
        ctrl.setAndMask(bitMask)
        stat    = afwMath.makeStatistics(im, mask, afwMath.STDEVCLIP | afwMath.MEDIAN | afwMath.NPOINT, ctrl)
        stdev   = stat.getValue(afwMath.STDEVCLIP)
        med     = stat.getValue(afwMath.MEDIAN)
    else:
        # It should be that afwMath.NPOINT = len(idx[0])
        # Not the case, exactly, so go with what you know
        idx     = np.where(mask.getArray() == 0)
        gdata   = im.getArray()[idx] 
        med     = np.median(gdata)
        stdev   = 0.741 * (np.percentile(gdata, 75) - np.percentile(gdata, 25))
    im -= med
    im /= stdev

    # Decision point: do I send the convolution a MaskedImage, in which
    # case the mask is also spread, or just an Image, and not spread
    # the mask...  
    # 
    # I think for now I will not spread the mask so that it represents the
    # condition of the underlying pixels, not the Psf-filtered ones
    # mi     = afwImage.MaskedImageF(im, mask, var)

    psf    = butler.get(datasetType="psField", dataId = dataId)
    wcs    = butler.get(datasetType="asTrans", dataId = dataId)

    # Image convolved with the Psf, i.e. maximum point source likelihood image
    cim    = afwImage.ImageF(im, True)
    afwMath.convolve(cim, im, psf.getKernel(), True)
    # The pixels that are "good" in the image, i.e. ignore borders
    cBBox  = psf.getKernel().shrinkBBox(cim.getBBox())
    cim    = afwImage.ImageF(cim, cBBox)
    mask   = afwImage.MaskU(mask, cBBox)
 
    # Create an ra,decl map for the good pixels
    raIm   = afwImage.ImageF(cim.getDimensions())
    decIm  = afwImage.ImageF(cim.getDimensions())
    nx, ny = cim.getDimensions()
    # But note that the Wcs expects their coordinates in the non-shrunk image
    x0     = cBBox.getBeginX()
    y0     = cBBox.getBeginY()
    x1     = cBBox.getEndX()
    y1     = cBBox.getEndY()
    for y in range(ny):
        for x in range(nx):
            ra, decl = getRaDecl(wcs, x+x0, y+y0)
            raIm.set(x, y, ra)
            decIm.set(x, y, decl) 

    run = dataId["run"]
    camcol = dataId["camcol"]
    field = dataId["field"]
    filterName = dataId["filter"]
    if doWriteSql:
        # Make the table inputs
        xll, yll = getRaDecl(wcs, 0 +x0, 0+ y0)
        xlr, ylr = getRaDecl(wcs, x1+x0, 0+ y0)
        xur, yur = getRaDecl(wcs, x1+x0, y1+y0)
        xul, yul = getRaDecl(wcs, 0 +x0, y1+y0)
        tc       = calib.getMidTime()
        t0       = dafBase.DateTime(tc.nsecs() - int(0.5 * calib.getExptime() * 1e9), dafBase.DateTime.TAI)
        t1       = dafBase.DateTime(tc.nsecs() + int(0.5 * calib.getExptime() * 1e9), dafBase.DateTime.TAI)

        # Magic for the day; 2**63 because BIGINT is signed
        fieldId  = int(md5.new(" ".join(map(str, [run, filterName, camcol, field]))).hexdigest(), 16) % 2**63

        pfile = "pixel-%06d-%s%s-%04d.pgsql" % (run, filterName, camcol, field)
        ffile = "field-%06d-%s%s-%04d.pgsql" % (run, filterName, camcol, field)
        pbuff = open(pfile, "w")
        fbuff = open(ffile, "w")
        fbuff.write("INSERT INTO fields (fieldId, run, camcol, field, filter, bbox, tmid, trange) VALUES\n")
        fbuff.write("  (%d, %d, %d, %d, '%s', ST_GeomFromText('POLYGON((\n" % (fieldId, run, camcol, field, filterName))
        fbuff.write("        %.6f %.6f, %.6f %.6f,\n" % (xll, yll, xlr, ylr))
        fbuff.write("        %.6f %.6f, %.6f %.6f,\n" % (xur, yur, xul, yul))
        fbuff.write("        %.6f %.6f))',4326),\n" % (xll, yll))
        fbuff.write("         '%s',\n" % (re.sub("T", " ", tc.toString())))
        fbuff.write("         '[%s, %s]');\n" % (re.sub("T", " ", t0.toString()), re.sub("T", " ", t1.toString())))

        pbuff.write("INSERT INTO pixels (fieldId, flux, mask) VALUES\n")
        for y in range(ny):
            for x in range(nx):
                if y==ny-1 and x==nx-1:
                    suffix = ";"
                else:
                    suffix = ","
                # Note the different orders of raIm,decIm and cim,mask
                pbuff.write("  (%d, ST_MakePointM(%f, %f, %f), %d)%s\n" % (
                    fieldId, raIm.get(x,y), decIm.get(x,y), cim.get(x,y), mask.get(x,y), suffix))
        pbuff.close()
        fbuff.close()

    cim.writeFits("image-%06d-%s%s-%04d.fits" % (run, filterName, camcol, field))
    mask.writeFits("mask-%06d-%s%s-%04d.fits" % (run, filterName, camcol, field))
    raIm.writeFits("ra-%06d-%s%s-%04d.fits" % (run, filterName, camcol, field))
    decIm.writeFits("dec-%06d-%s%s-%04d.fits" % (run, filterName, camcol, field))


if __name__ == "__main__":
    args = []
    dataIds = [{"run": 6474, "camcol": 5, "field": 143},
               {"run": 6484, "camcol": 5, "field": 144},
               {"run": 6504, "camcol": 5, "field": 146}]

    for dataId in dataIds:
        for filterName in ["g", "r", "i"]:
            dId = dataId.copy()
            dId["filter"] = filterName
            args.append(dId)

    print args
    if True:
        # 1 by 1
        map(doit, args)
    else:
        # In parallel; note you need to import this *after* you declare doit()
        import multiprocessing
        pool = multiprocessing.Pool(multiprocessing.cpu_count()//2)
        pool.map(doit, args)
