from __future__ import absolute_import

import os.path
from collections import namedtuple
from skimage import draw
from scipy.ndimage import filters, morphology
import cv2
import numpy as np
from shapely.geometry import Polygon, LineString

from ocrd import Processor
from ocrd_utils import (
    getLogger, concat_padded,
    coordinates_for_segment,
    coordinates_of_segment,
    polygon_from_points,
    points_from_polygon,
    xywh_from_polygon,
    MIMETYPE_PAGE
)
from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import (
    CoordsType,
    LabelType, LabelsType,
    MetadataItemType,
    to_xml
)
from ocrd_models.ocrd_page_generateds import (
    RegionRefType,
    RegionRefIndexedType,
    OrderedGroupType,
    OrderedGroupIndexedType,
    UnorderedGroupType,
    UnorderedGroupIndexedType,
    ReadingOrderType
)
from .config import OCRD_TOOL

TOOL = 'ocrd-segment-repair'
LOG = getLogger('processor.RepairSegmentation')

class RepairSegmentation(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super(RepairSegmentation, self).__init__(*args, **kwargs)


    def process(self):
        """Performs segmentation evaluation with Shapely on the workspace.
        
        Open and deserialize PAGE input files and their respective images,
        then iterate over the element hierarchy down to the region level.
        
        Return information on the plausibility of the segmentation into
        regions on the logging level.
        """
        sanitize = self.parameter['sanitize']
        plausibilize = self.parameter['plausibilize']
        
        for (n, input_file) in enumerate(self.input_files):
            page_id = input_file.pageId or input_file.ID
            LOG.info("INPUT FILE %i / %s", n, page_id)
            pcgts = page_from_file(self.workspace.download_file(input_file))
            page = pcgts.get_Page()
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

            #
            # validate segmentation (warn of children extending beyond their parents)
            #
            self.validate_coords(page, page_id)

            #
            # sanitize region segmentation (shrink to hull of lines)
            #
            if sanitize:
                self.sanitize_page(page, page_id)
                
            #
            # plausibilize region segmentation (remove redundant text regions)
            #
            mark_for_deletion = list() # what regions get removed?
            mark_for_merging = dict() # what regions get merged into which regions?

            # TODO: cover recursive region structure (but compare only at the same level)
            regions = page.get_TextRegion()
            # sort by area to ensure to arrive at a total ordering compatible
            # with the topological sort along containment/equivalence arcs
            # (so we can avoid substituting regions with superregions that have
            #  themselves been substituted/deleted):
            RegionPolygon = namedtuple('RegionPolygon', ['region', 'polygon'])
            regionspolys = sorted([RegionPolygon(region, Polygon(polygon_from_points(region.get_Coords().points)))
                                   for region in regions],
                                  key=lambda x: x.polygon.area)
            for i in range(0, len(regionspolys)):
                for j in range(i+1, len(regionspolys)):
                    region1 = regionspolys[i].region
                    region2 = regionspolys[j].region
                    poly1 = regionspolys[i].polygon
                    poly2 = regionspolys[j].polygon
                    LOG.debug('Comparing regions "%s" and "%s"', region1.id, region2.id)
                    
                    if poly1.almost_equals(poly2):
                        LOG.warning('Page "%s" region "%s" is almost equal to "%s" %s',
                                    page_id, region2.id, region1.id,
                                    '(removing)' if plausibilize else '')
                        mark_for_deletion.append(region2.id)
                    elif poly1.contains(poly2):
                        LOG.warning('Page "%s" region "%s" is within "%s" %s',
                                    page_id, region2.id, region1.id,
                                    '(removing)' if plausibilize else '')
                        mark_for_deletion.append(region2.id)
                    elif poly2.contains(poly1):
                        LOG.warning('Page "%s" region "%s" is within "%s" %s',
                                    page_id, region1.id, region2.id,
                                    '(removing)' if plausibilize else '')
                        mark_for_deletion.append(region1.id)
                    elif poly1.overlaps(poly2):
                        inter_poly = poly1.intersection(poly2)
                        union_poly = poly1.union(poly2)
                        LOG.debug('Page "%s" region "%s" overlaps "%s" by %f/%f',
                                  page_id, region1.id, region2.id, inter_poly.area/poly1.area, inter_poly.area/poly2.area)
                        if union_poly.convex_hull.area >= poly1.area + poly2.area:
                            # skip this pair -- combined polygon encloses previously free segments
                            pass
                        elif inter_poly.area / poly2.area > self.parameter['plausibilize_merge_min_overlap']:
                            LOG.warning('Page "%s" region "%s" is almost within "%s" %s',
                                        page_id, region2.id, region1.id,
                                        '(merging)' if plausibilize else '')
                            mark_for_merging[region2.id] = region1
                        elif inter_poly.area / poly1.area > self.parameter['plausibilize_merge_min_overlap']:
                            LOG.warning('Page "%s" region "%s" is almost within "%s" %s',
                                        page_id, region1.id, region2.id,
                                        '(merging)' if plausibilize else '')
                            mark_for_merging[region1.id] = region2

                    # TODO: more merging cases...
                    #LOG.info('Intersection %i', poly1.intersects(poly2))
                    #LOG.info('Containment %i', poly1.contains(poly2))
                    #if poly1.intersects(poly2):
                    #    LOG.info('Area 1 %d', poly1.area)
                    #    LOG.info('Area 2 %d', poly2.area)
                    #    LOG.info('Area intersect %d', poly1.intersection(poly2).area)
                        

            if plausibilize:
                # the reading order does not have to include all regions
                # but it may include all types of regions!
                ro = page.get_ReadingOrder()
                if ro:
                    rogroup = ro.get_OrderedGroup() or ro.get_UnorderedGroup()
                else:
                    rogroup = None
                # pass the regions sorted (see above)
                _plausibilize_group(regionspolys, rogroup, mark_for_deletion, mark_for_merging)

            # Use input_file's basename for the new file -
            # this way the files retain the same basenames:
            file_id = input_file.ID.replace(self.input_file_grp, self.output_file_grp)
            if file_id == input_file.ID:
                file_id = concat_padded(self.output_file_grp, n)
            self.workspace.add_file(
                ID=file_id,
                file_grp=self.output_file_grp,
                pageId=input_file.pageId,
                mimetype=MIMETYPE_PAGE,
                local_filename=os.path.join(self.output_file_grp,
                                            file_id + '.xml'),
                content=to_xml(pcgts))
    
    def sanitize_page(self, page, page_id):
        regions = page.get_TextRegion()
        page_image, page_coords, _ = self.workspace.image_from_page(
            page, page_id)
        for region in regions:
            LOG.info('Sanitizing region "%s"', region.id)
            lines = region.get_TextLine()
            if not lines:
                LOG.warning('Page "%s" region "%s" contains no textlines', page_id, region.id)
                continue
            heights = []
            tops = []
            # get labels:
            region_mask = np.zeros((page_image.height, page_image.width), dtype=np.uint8)
            for line in lines:
                line_polygon = coordinates_of_segment(line, page_image, page_coords)
                line_xywh = xywh_from_polygon(line_polygon)
                heights.append(line_xywh['h'])
                tops.append(line_xywh['y'])
                region_mask[draw.polygon(line_polygon[:, 1],
                                         line_polygon[:, 0],
                                         region_mask.shape)] = 1
                region_mask[draw.polygon_perimeter(line_polygon[:, 1],
                                                   line_polygon[:, 0],
                                                   region_mask.shape)] = 1
            # estimate scale:
            heights = np.array(heights)
            scale = int(np.max(heights))
            tops = np.array(tops)
            order = np.argsort(tops)
            heights = heights[order]
            tops = tops[order]
            if len(lines) > 1:
                # if interline spacing is larger than line height, use this
                bottoms = tops + heights
                deltas = tops[1:] - bottoms[:-1]
                scale = max(scale, int(np.max(deltas)))
            # close labels:
            region_mask = np.pad(region_mask, scale) # protect edges
            region_mask = np.array(morphology.binary_closing(region_mask, np.ones((scale, 1))), dtype=np.uint8)
            region_mask = region_mask[scale:-scale, scale:-scale] # unprotect
            # extend margins (to ensure simplified hull polygon is outside children):
            region_mask = filters.maximum_filter(region_mask, 3) # 1px in each direction
            # find outer contour (parts):
            contours, _ = cv2.findContours(region_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            # determine areas of parts:
            areas = [cv2.contourArea(contour) for contour in contours]
            total_area = sum(areas)
            if not total_area:
                # ignore if too small
                LOG.warning('Zero contour area in region "%s"', region.id)
                continue
            # pick contour and convert to absolute:
            region_polygon = None
            for i, contour in enumerate(contours):
                area = areas[i]
                if area / total_area < 0.1:
                    LOG.warning('Ignoring contour %d too small (%d/%d) in region "%s"',
                                i, area, total_area, region.id)
                    continue
                # simplify shape:
                # can produce invalid (self-intersecting) polygons:
                #polygon = cv2.approxPolyDP(contour, 2, False)[:, 0, ::] # already ordered x,y
                polygon = contour[:, 0, ::] # already ordered x,y
                polygon = Polygon(polygon).simplify(1).exterior.coords
                if len(polygon) < 4:
                    LOG.warning('Ignoring contour %d less than 4 points in region "%s"',
                                i, region.id)
                    continue
                if region_polygon is not None:
                    LOG.error('Skipping region "%s" due to non-contiguous contours',
                              region.id)
                    region_polygon = None
                    break
                region_polygon = coordinates_for_segment(polygon, page_image, page_coords)
            if region_polygon is not None:
                LOG.info('Using new coordinates for region "%s"', region.id)
                region.get_Coords().points = points_from_polygon(region_polygon)
    
    def validate_coords(self, page, page_id):
        valid = True
        regions = page.get_TextRegion()
        if page.get_Border():
            other_regions = (
                page.get_AdvertRegion() +
                page.get_ChartRegion() +
                page.get_ChemRegion() +
                page.get_GraphicRegion() +
                page.get_ImageRegion() +
                page.get_LineDrawingRegion() +
                page.get_MathsRegion() +
                page.get_MusicRegion() +
                page.get_NoiseRegion() +
                page.get_SeparatorRegion() +
                page.get_TableRegion() +
                page.get_UnknownRegion())
            for region in regions + other_regions:
                if not _child_within_parent(region, page.get_Border()):
                    LOG.warning('Region "%s" extends beyond Border of page "%s"',
                                region.id, page_id)
                    valid = False
        for region in regions:
            lines = region.get_TextLine()
            for line in lines:
                if not _child_within_parent(line, region):
                    LOG.warning('Line "%s" extends beyond region "%s" on page "%s"',
                                line.id, region.id, page_id)
                    valid = False
                if line.get_Baseline():
                    baseline = LineString(polygon_from_points(line.get_Baseline().points))
                    linepoly = Polygon(polygon_from_points(line.get_Coords().points))
                    if not baseline.within(linepoly):
                        LOG.warning('Baseline extends beyond line "%s" in region "%s" on page "%s"',
                                    line.id, region.id, page_id)
                        valid = False
                words = line.get_Word()
                for word in words:
                    if not _child_within_parent(word, line):
                        LOG.warning('Word "%s" extends beyond line "%s" in region "%s" on page "%s"',
                                    word.id, line.id, region.id, page_id)
                        valid = False
                    glyphs = word.get_Glyph()
                    for glyph in glyphs:
                        if not _child_within_parent(glyph, word):
                            LOG.warning('Glyph "%s" extends beyond word "%s" in line "%s" of region "%s" on page "%s"',
                                        glyph.id, word.id, line.id, region.id, page_id)
                            valid = False
        return valid

def _child_within_parent(child, parent):
    child_poly = Polygon(polygon_from_points(child.get_Coords().points))
    parent_poly = Polygon(polygon_from_points(parent.get_Coords().points))
    return child_poly.within(parent_poly)

def _plausibilize_group(regionspolys, rogroup, mark_for_deletion, mark_for_merging):
    wait_for_deletion = list()
    reading_order = dict()
    regionrefs = list()
    ordered = False
    if isinstance(rogroup, (OrderedGroupType, OrderedGroupIndexedType)):
        regionrefs = (rogroup.get_RegionRefIndexed() +
                      rogroup.get_OrderedGroupIndexed() +
                      rogroup.get_UnorderedGroupIndexed())
        ordered = True
    if isinstance(rogroup, (UnorderedGroupType, UnorderedGroupIndexedType)):
        regionrefs = (rogroup.get_RegionRef() +
                      rogroup.get_OrderedGroup() +
                      rogroup.get_UnorderedGroup())
    for elem in regionrefs:
        reading_order[elem.get_regionRef()] = elem
        if not isinstance(elem, (RegionRefType, RegionRefIndexedType)):
            # recursive reading order element (un/ordered group):
            _plausibilize_group(regionspolys, elem, mark_for_deletion, mark_for_merging)
    for regionpoly in regionspolys:
        delete = regionpoly.region.id in mark_for_deletion
        merge = regionpoly.region.id in mark_for_merging
        if delete or merge:
            region = regionpoly.region
            poly = regionpoly.polygon
            if merge:
                # merge region with super region:
                superreg = mark_for_merging[region.id]
                # granularity will necessarily be lost here --
                # this is not for workflows/processors that already
                # provide good/correct segmentation and reading order
                # (in which case orientation, script and style detection
                #  can be expected as well), but rather as a postprocessor
                # for suboptimal segmentation (possibly before reading order
                # detection/correction); hence, all we now do here is
                # show warnings when granularity is lost; but there might
                # be good reasons to do more here when we have better processors
                # and use-cases in the future
                superpoly = Polygon(polygon_from_points(superreg.get_Coords().points))
                superpoly = superpoly.union(poly)
                superreg.get_Coords().points = points_from_polygon(superpoly.exterior.coords)
                # FIXME should we merge/mix attributes and features?
                if region.get_orientation() != superreg.get_orientation():
                    LOG.warning('Merging region "%s" with orientation %f into "%s" with %f',
                                region.id, region.get_orientation(),
                                superreg.id, superreg.get_orientation())
                if region.get_type() != superreg.get_type():
                    LOG.warning('Merging region "%s" with type %s into "%s" with %s',
                                region.id, region.get_type(),
                                superreg.id, superreg.get_type())
                if region.get_primaryScript() != superreg.get_primaryScript():
                    LOG.warning('Merging region "%s" with primaryScript %s into "%s" with %s',
                                region.id, region.get_primaryScript(),
                                superreg.id, superreg.get_primaryScript())
                if region.get_primaryLanguage() != superreg.get_primaryLanguage():
                    LOG.warning('Merging region "%s" with primaryLanguage %s into "%s" with %s',
                                region.id, region.get_primaryLanguage(),
                                superreg.id, superreg.get_primaryLanguage())
                if region.get_TextStyle():
                    LOG.warning('Merging region "%s" with TextStyle %s into "%s" with %s',
                                region.id, region.get_TextStyle(), # FIXME needs repr...
                                superreg.id, superreg.get_TextStyle()) # ...to be informative
                if region.get_TextEquiv():
                    LOG.warning('Merging region "%s" with TextEquiv %s into "%s" with %s',
                                region.id, region.get_TextEquiv(), # FIXME needs repr...
                                superreg.id, superreg.get_TextEquiv()) # ...to be informative
            wait_for_deletion.append(region)
            if region.id in reading_order:
                regionref = reading_order[region.id]
                # TODO: re-assign regionref.continuation and regionref.type to other?
                # could be any of the 6 types above:
                regionrefs = rogroup.__getattribute__(regionref.__class__.__name__.replace('Type', ''))
                # remove in-place
                regionrefs.remove(regionref)

    if ordered:
        # re-index the reading order!
        regionrefs.sort(key=RegionRefIndexedType.get_index)
        for i, regionref in enumerate(regionrefs):
            regionref.set_index(i)
        
    for region in wait_for_deletion:
        if region.parent_object_:
            # remove in-place
            region.parent_object_.get_TextRegion().remove(region)
