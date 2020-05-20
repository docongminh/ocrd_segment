from __future__ import absolute_import

import json
import os.path
import os
import numpy as np
from shapely.geometry import Polygon
from shapely.prepared import prep
import cv2
from PIL import Image, ImageDraw

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' # i.e. error
from mrcnn import model
from mrcnn.config import Config
import tensorflow as tf
tf.get_logger().setLevel('ERROR')

from ocrd_utils import (
    getLogger, concat_padded,
    coordinates_of_segment,
    coordinates_for_segment,
    polygon_from_bbox,
    points_from_polygon,
    MIMETYPE_PAGE
)
from ocrd_models.ocrd_page import (
    MetadataItemType,
    LabelsType, LabelType,
    to_xml, TextRegionType,
    CoordsType
)
from ocrd_models.ocrd_page_generateds import (
    RegionRefType,
    RegionRefIndexedType,
    OrderedGroupType,
    OrderedGroupIndexedType,
    UnorderedGroupType,
    UnorderedGroupIndexedType,
    PageType, TextEquivType
)
from ocrd_modelfactory import page_from_file
from ocrd import Processor

from .config import OCRD_TOOL

TOOL = 'ocrd-segment-classify-address-layout'
LOG = getLogger('processor.ClassifyAddressLayout')

class AddressConfig(Config):
    """Configuration for detection on address resegmentation"""
    NAME = "address"
    IMAGES_PER_GPU = 1
    GPU_COUNT = 1
    BACKBONE = "resnet50"
    # Number of classes (including background)
    NUM_CLASSES = 3 + 1  # new address model has bg + 3 classes (rcpt/sndr/contact)
    #NUM_CLASSES = 1 + 1  # old address model has bg + 1 classes (rcpt)
    DETECTION_MAX_INSTANCES = 5
    DETECTION_MIN_CONFIDENCE = 0.7
    PRE_NMS_LIMIT = 2000
    POST_NMS_ROIS_INFERENCE = 500
    IMAGE_RESIZE_MODE = "square"
    IMAGE_MIN_DIM = 600
    IMAGE_MAX_DIM = 768
    IMAGE_CHANNEL_COUNT = 4
    MEAN_PIXEL = np.array([123.7, 116.8, 103.9, 0])

class ClassifyAddressLayout(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super(ClassifyAddressLayout, self).__init__(*args, **kwargs)
        self.categories = ['',
                           'address-rcpt',
                           'address-sndr',
                           'address-contact']
        if hasattr(self, 'output_file_grp'):
            def readable(path):
                return os.path.isfile(path) and os.access(path, os.R_OK)
            directories = ['', os.path.dirname(os.path.abspath(__file__))]
            if 'MRCNNDATA' in os.environ:
                directories = [os.environ['MRCNNDATA']] + directories
            model_path = ''
            for directory in directories:
                if readable(os.path.join(directory, self.parameter['model'])):
                    model_path = os.path.join(directory, self.parameter['model'])
                    break
            if not model_path:
                raise Exception("model file '%s' not found", self.parameter['model'])
            LOG.info("Loading model '%s'", model_path)
            config = AddressConfig()
            config.DETECTION_MIN_CONFIDENCE = self.parameter['min_confidence']
            #config.display()
            self.model = model.MaskRCNN(
                mode="inference", config=config,
                # not really needed, but must be a path...
                model_dir=os.getcwd())
            self.model.load_weights(model_path, by_name=True)

    def process(self):
        """Detect and classify+resegment address regions from text recognition results.
        
        Open and deserialize PAGE input files and their respective images,
        then iterate over the element hierarchy down to the text line level.
        
        Then, get the text classification result for each text line (what
        parts of address descriptions it contains, if any).
        
        Next, retrieve the page image according to the layout annotation (from
        the alternative image of the page, or by cropping at Border and deskewing
        according to @orientation) in raw RGB form. Represent it as an array with
        an alpha channel where the text lines are marked according to their class.
        
        Pass that array to a visual address detector model, and retrieve region
        candidates as tuples of region class, bounding box, and pixel mask.
        Postprocess the mask and bbox to ensure no words are cut off accidentally.
        
        Where the class confidence is high enough, annotate the resulting TextRegion
        (including the special address type), and remove any overlapping input regions
        (re-assigning its text lines).
        
        Produce a new output file by serialising the resulting hierarchy.
        """
        
        # pylint: disable=attribute-defined-outside-init
        for n, input_file in enumerate(self.input_files):
            file_id = input_file.ID.replace(self.input_file_grp, self.output_file_grp)
            if file_id == input_file.ID:
                file_id = concat_padded(self.output_file_grp, n)
            page_id = input_file.pageId or input_file.ID
            LOG.info("INPUT FILE %i / %s", n, page_id)
            pcgts = page_from_file(self.workspace.download_file(input_file))
            
            # add metadata about this operation and its runtime parameters:
            metadata = pcgts.get_Metadata() # ensured by from_file()
            metadata.add_MetadataItem(
                MetadataItemType(type_="processingStep",
                                 name=self.ocrd_tool['steps'][0],
                                 value=TOOL,
                                 Labels=[LabelsType(
                                     externalModel="ocrd-tool",
                                     externalId="parameters",
                                     Label=[LabelType(type_=name,
                                                      value=self.parameter[name])
                                            for name in self.parameter.keys()])]))
            
            page = pcgts.get_Page()
            page_image, page_coords, page_image_info = self.workspace.image_from_page(
                page, page_id,
                feature_filter='binarized',
                transparency=False)
            if page_image_info.resolution != 1:
                dpi = page_image_info.resolution
                if page_image_info.resolutionUnit == 'cm':
                    dpi = round(dpi * 2.54)
            else:
                dpi = None
            # page_image_binarized, _, _ = self.workspace.image_from_page(
            #     page, page_id,
            #     feature_selector='binarized')
            
            # ensure RGB (if raw was merely grayscale)
            page_image = page_image.convert(mode='RGB')
            # prepare mask image (alpha channel for input image)
            page_image_mask = Image.new(mode='L', size=page_image.size, color=0)
            def mark_line(line):
                text_class = line.get_custom()
                if not text_class:
                    return
                text_class = text_class.replace('subtype: ', '')
                if (not text_class.startswith('ADDRESS_') or
                    text_class == 'ADDRESS_NONE'):
                    return
                # add to mask image (alpha channel for input image)
                polygon = coordinates_of_segment(line, page_image, page_coords)
                # draw line mask:
                ImageDraw.Draw(page_image_mask).polygon(
                    list(map(tuple, polygon.tolist())),
                    fill=200 if text_class == 'ADDRESS_NONE' else 255)
            
            # prepare reading order
            reading_order = dict()
            ro = page.get_ReadingOrder()
            if ro:
                rogroup = ro.get_OrderedGroup() or ro.get_UnorderedGroup()
                if rogroup:
                    page_get_reading_order(reading_order, rogroup)
            
            # iterate through all regions that could have lines
            allregions = page_get_all_regions(page, classes='Text', order='document', depth=2)
            allpolys = [prep(Polygon(coordinates_of_segment(region, page_image, page_coords)))
                        for region in allregions]
            for region in allregions:
                for line in region.get_TextLine():
                    mark_line(line)
            
            # combine raw with aggregated mask to RGBA array
            page_image.putalpha(page_image_mask)
            page_array = np.array(page_image)
            # predict
            preds = self.model.detect([page_array], verbose=0)[0]
            worse = []
            for i in range(len(preds['class_ids'])):
                for j in range(i + 1, len(preds['class_ids'])):
                    imask = preds['masks'][:,:,i]
                    jmask = preds['masks'][:,:,j]
                    if np.any(imask * jmask):
                        worse.append(i if preds['scores'][i] < preds['scores'][j] else j)
            best = np.zeros(4)
            for i in range(len(preds['class_ids'])):
                if i in worse:
                    continue
                cat = preds['class_ids'][i]
                score = preds['scores'][i]
                if cat not in [1,2]:
                    # only best probs for sndr and rcpt (other can be many)
                    continue
                if score > best[cat]:
                    best[cat] = score
            if not np.any(best):
                LOG.warning("Detected no sndr/rcpt address on page '%s'", page_id)
            for i in range(len(preds['class_ids'])):
                if i in worse:
                    continue
                cat = preds['class_ids'][i]
                score = preds['scores'][i]
                if not cat:
                    raise Exception('detected region for background class')
                if score < best[cat]:
                    # ignore non-best
                    continue
                name = self.categories[cat]
                mask = preds['masks'][:,:,i]
                bbox = np.around(preds['rois'][i])
                area = np.count_nonzero(mask)
                scale = int(np.sqrt(area)//10)
                scale = scale + (scale+1)%2 # odd
                LOG.debug("post-processing prediction for '%s' at %s area %d score %f",
                          name, str(bbox), area, score)
                # dilate and find (outer) contour
                contours = [None, None]
                for _ in range(10):
                    if len(contours) == 1:
                        break
                    mask = cv2.dilate(mask.astype(np.uint8),
                                      np.ones((scale,scale), np.uint8)) > 0
                    contours, _ = cv2.findContours(mask.astype(np.uint8),
                                                   cv2.RETR_EXTERNAL,
                                                   cv2.CHAIN_APPROX_SIMPLE)
                region_poly = Polygon(contours[0][:,0,:]) # already in x,y order
                for tolerance in range(2, int(area)):
                    region_poly = region_poly.simplify(tolerance)
                    if region_poly.is_valid:
                        break
                region_polygon = region_poly.exterior.coords[:-1] # keep open
                #region_polygon = polygon_from_bbox(bbox[1],bbox[0],bbox[3],bbox[2])
                # TODO: post-process (closure/majority in binarized, then clip to parent/border)
                # annotate new region
                region_polygon = coordinates_for_segment(region_polygon,
                                                         page_image, page_coords)
                region_coords = CoordsType(points_from_polygon(region_polygon), conf=score)
                region_id = 'addressregion%02d' % (i+1)
                region = TextRegionType(id=region_id,
                                        Coords=region_coords,
                                        type_='other',
                                        custom='subtype:' + name)
                LOG.info("Detected %s region '%s' on page '%s'",
                         name, region_id, page_id)
                has_address = False
                oldregions = []
                # remove overlapping existing regions
                for neighbour, neighpoly in zip(allregions, allpolys):
                    if neighbour in oldregions:
                        continue
                    if (neighpoly.within(region_poly) or
                        neighpoly.within(region_poly.buffer(4*scale)) or
                        (neighpoly.intersects(region_poly) and (
                            neighpoly.context.almost_equals(region_poly) or
                            neighpoly.context.intersection(region_poly).area > 0.8 * neighpoly.context.area))):
                        LOG.debug("removing redundant region '%s' in favour of '%s'",
                                  neighbour.id, region.id)
                        # re-assign text lines
                        line_no = len(region.get_TextLine())
                        for line in neighbour.get_TextLine():
                            if line.get_custom() and line.get_custom().startswith('subtype: ADDRESS_'):
                                has_address = True
                            LOG.debug("stealing text line '%s'", line.id)
                            line.id = region.id + '_line%02d' % line_no
                            line_no += 1
                            region.add_TextLine(line)
                            line_poly = Polygon(coordinates_of_segment(
                                line, page_image, page_coords))
                            if not line_poly.within(region_poly):
                                region_poly = line_poly.union(region_poly)
                                if region_poly.type == 'MultiPolygon':
                                    region_poly = region_poly.convex_hull
                                region_polygon = coordinates_for_segment(
                                    region_poly.exterior.coords[:-1], page_image, page_coords)
                                region.get_Coords().points = points_from_polygon(region_polygon)
                        region.set_TextEquiv([TextEquivType(Unicode='\n'.join(
                            line.get_TextEquiv()[0].Unicode for line in region.get_TextLine()
                            if line.get_TextEquiv()))])
                        # don't re-assign by another address detection
                        oldregions.append(neighbour)
                        # remove old region
                        neighbour.parent_object_.TextRegion.remove(neighbour)
                        if neighbour.id in reading_order:
                            roelem = reading_order[neighbour.id]
                            roelem.set_regionRef(region.id)
                            reading_order[region.id] = roelem
                            del reading_order[neighbour.id]
                    elif neighpoly.crosses(region_poly):
                        LOG.debug("ignoring crossing region '%s' for '%s'",
                                  neighbour.id, region.id)
                    elif neighpoly.overlaps(region_poly):
                        LOG.debug("ignoring overlapping region '%s' for '%s'",
                                  neighbour.id, region.id)
                # safe-guard against ghost detections:
                if has_address:
                    page.add_TextRegion(region)
                else:
                    LOG.info("Ignoring %s region '%s' without any address lines",
                             name, region_id)
            
            file_path = os.path.join(self.output_file_grp,
                                     file_id + '.xml')
            out = self.workspace.add_file(
                ID=file_id,
                file_grp=self.output_file_grp,
                pageId=input_file.pageId,
                local_filename=file_path,
                mimetype=MIMETYPE_PAGE,
                content=to_xml(pcgts))
            LOG.info('created file ID: %s, file_grp: %s, path: %s',
                     file_id, self.output_file_grp, out.local_filename)

# FIXME: belongs into OCR-D/core
def page_get_all_regions(page, classes=None, order='document', depth=1):
    """
    Get all *Region elements below ``page``,
    or only those provided by ``classes``,
    returned in the order specified by ``reading_order``,
    and up to ``depth`` levels of recursion.
    Arguments:
       * ``classes`` (list) Classes of regions that shall be returned, e.g. ['Text', 'Image']
       * ``order`` ('document'|'reading-order') Whether to return regions sorted by document order (default) or by reading order
       * ``depth`` (integer) Maximum recursion level. Use 0 for arbitrary (i.e. unbounded) depth.
   
   For example, to get all text anywhere on the page in reading order, use:
   ::
       '\n'.join(line.get_TextEquiv()[0].Unicode
                 for region in page_get_all_regions(page, classes='Text', depth=0, order='reading-order')
                 for line in region.get_TextLine())
    """
    def region_class(x):
        return x.__class__.__name__.replace('RegionType', '')
    
    def get_recursive_regions(regions, level):
        if level == 1:
            # stop recursion, filter classes
            if classes:
                return [r for r in regions if region_class(r) in classes]
            else:
                return regions
        # find more regions recursively
        more_regions = []
        for region in regions:
            more_regions.append([])
            for class_ in ['Advert', 'Chart', 'Chem', 'Custom', 'Graphic', 'Image', 'LineDrawing', 'Map', 'Maths', 'Music', 'Noise', 'Separator', 'Table', 'Text', 'Unknown']:
                if class_ == 'Map' and not isinstance(region, PageType):
                    # 'Map' is not recursive in 2019 schema
                    continue
                more_regions[-1] += getattr(region, 'get_{}Region'.format(class_))()
        if not any(more_regions):
            return get_recursive_regions(regions, 1)
        regions = [region for r, more in zip(regions, more_regions) for region in [r] + more]
        return get_recursive_regions(regions, level - 1 if level else 0)
    ret = get_recursive_regions([page], depth + 1 if depth else 0)
    if order == 'reading-order':
        reading_order = page.get_ReadingOrder()
        if reading_order:
            reading_order = reading_order.get_OrderedGroup() or reading_order.get_UnorderedGroup()
        if reading_order:
            def get_recursive_reading_order(rogroup):
                if isinstance(rogroup, (OrderedGroupType, OrderedGroupIndexedType)):
                    elements = sorted(rogroup.get_RegionRefIndexed() +
                                      rogroup.get_OrderedGroupIndexed() + rogroup.get_UnorderedGroupIndexed(),
                                      key=lambda x : x.index)
                if isinstance(rogroup, (UnorderedGroupType, UnorderedGroupIndexedType)):
                    elements = (rogroup.get_RegionRef() + rogroup.get_OrderedGroup() + rogroup.get_UnorderedGroup())
                regionrefs = list()
                for elem in elements:
                    regionrefs.append(elem.get_regionRef())
                    if not isinstance(elem, (RegionRefType, RegionRefIndexedType)):
                        regionrefs.extend(get_recursive_reading_order(elem))
                return regionrefs
            reading_order = get_recursive_reading_order(reading_order)
        if reading_order:
            id2region = dict([(region.id, region) for region in ret])
            ret = [id2region[region_id] for region_id in reading_order if region_id in id2region]
    ret = [r for r in ret if region_class(r) in classes]
    return ret

def page_get_reading_order(ro, rogroup):
    """Add all elements from the given reading order group to the given dictionary.
    
    Given a dict ``ro`` from layout element IDs to ReadingOrder element objects,
    and an object ``rogroup`` with additional ReadingOrder element objects,
    add all references to the dict, traversing the group recursively.
    """
    if isinstance(rogroup, (OrderedGroupType, OrderedGroupIndexedType)):
        regionrefs = (rogroup.get_RegionRefIndexed() +
                      rogroup.get_OrderedGroupIndexed() +
                      rogroup.get_UnorderedGroupIndexed())
    if isinstance(rogroup, (UnorderedGroupType, UnorderedGroupIndexedType)):
        regionrefs = (rogroup.get_RegionRef() +
                      rogroup.get_OrderedGroup() +
                      rogroup.get_UnorderedGroup())
    for elem in regionrefs:
        ro[elem.get_regionRef()] = elem
        if not isinstance(elem, (RegionRefType, RegionRefIndexedType)):
            page_get_reading_order(ro, elem)