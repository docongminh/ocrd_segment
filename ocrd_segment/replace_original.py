from __future__ import absolute_import

import os.path

from ocrd_utils import (
    getLogger, concat_padded,
    coordinates_of_segment,
    points_from_polygon,
    MIMETYPE_PAGE
)
from ocrd_models.ocrd_page import (
    LabelsType, LabelType,
    MetadataItemType,
    TextRegionType,
    to_xml
)
from ocrd_modelfactory import page_from_file
from ocrd import Processor

from .config import OCRD_TOOL

TOOL = 'ocrd-segment-replace-original'
LOG = getLogger('processor.ReplaceOriginal')
FALLBACK_FILEGRP_IMG = 'OCR-D-IMG-SUBST'

class ReplaceOriginal(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super(ReplaceOriginal, self).__init__(*args, **kwargs)

    def process(self):
        """Extract page image and replace original with it.
        
        Open and deserialize PAGE input files and their respective images,
        then go to the page hierarchy level.
        
        Retrieve the image of the (cropped, deskewed, dewarped) page, preferring
        the last annotated form (which, depending on the workflow, could be
        binarized or raw). Add that image file to the workspace with the fileGrp
        USE given in the second position of the output fileGrp, or ``OCR-D-IMG-SUBST``.
        Reference that file in the page (not as AlternativeImage but) as original
        image. Adjust all segment coordinates accordingly.
        
        Produce a new output file by serialising the resulting hierarchy.
        """
        try:
            page_grp, image_grp = self.output_file_grp.split(',')
        except ValueError:
            page_grp = self.output_file_grp
            image_grp = FALLBACK_FILEGRP_IMG
            LOG.info("No output file group for images specified, falling back to '%s'", image_grp)
        feature_selector = self.parameter['feature_selector']
        feature_filter = self.parameter['feature_filter']
        adapt_coords = self.parameter['transform_coordinates']
        
        # pylint: disable=attribute-defined-outside-init
        for n, input_file in enumerate(self.input_files):
            file_id = input_file.ID.replace(self.input_file_grp, page_grp)
            if file_id == input_file.ID:
                file_id = concat_padded(page_grp, n)
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
                                            for name in self.parameter])]))
            page_image, page_coords, page_image_info = self.workspace.image_from_page(
                page, page_id,
                feature_filter=feature_filter,
                feature_selector=feature_selector)
            if page_image_info.resolution != 1:
                dpi = page_image_info.resolution
                if page_image_info.resolutionUnit == 'cm':
                    dpi = round(dpi * 2.54)
            else:
                dpi = None
            # annotate extracted image
            file_path = self.workspace.save_image_file(page_image,
                                                       file_id.replace(page_grp, image_grp),
                                                       image_grp,
                                                       page_id=input_file.pageId,
                                                       mimetype='image/png')
            # replace original image
            page.set_imageFilename(file_path)
            # adjust all coordinates
            if adapt_coords:
                for region in page.get_AllRegions():
                    region_polygon = coordinates_of_segment(region, page_image, page_coords)
                    region.get_Coords().points = points_from_polygon(region_polygon)
                    if isinstance(region, TextRegionType):
                        for line in region.get_TextLine():
                            line_polygon = coordinates_of_segment(line, page_image, page_coords)
                            line.get_Coords().points = points_from_polygon(line_polygon)
                            for word in line.get_Word():
                                word_polygon = coordinates_of_segment(word, page_image, page_coords)
                                word.get_Coords().points = points_from_polygon(word_polygon)
                                for glyph in word.get_Glyph():
                                    glyph_polygon = coordinates_of_segment(glyph, page_image, page_coords)
                                    glyph.get_Coords().points = points_from_polygon(glyph_polygon)
            
            # update METS (add the PAGE file):
            file_path = os.path.join(page_grp, file_id + '.xml')
            out = self.workspace.add_file(
                ID=file_id,
                file_grp=page_grp,
                pageId=input_file.pageId,
                local_filename=file_path,
                mimetype=MIMETYPE_PAGE,
                content=to_xml(pcgts))
            LOG.info('created file ID: %s, file_grp: %s, path: %s',
                     file_id, page_grp, out.local_filename)
