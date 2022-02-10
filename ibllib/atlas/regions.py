from dataclasses import dataclass
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from iblutil.util import Bunch
from iblutil.numerical import ismember

_logger = logging.getLogger('ibllib')
# 'Beryl' is the name given to an atlas containing a subset of the most relevant allen annotations
FILE_BERYL = str(Path(__file__).parent.joinpath('beryl.npy'))
FILE_COSMOS = str(Path(__file__).parent.joinpath('cosmos.npy'))
FILE_MAPPINGS = str(Path(__file__).parent.joinpath('mappings.pqt'))
FILE_REGIONS = str(Path(__file__).parent.joinpath('allen_structure_tree.csv'))


@dataclass
class _BrainRegions:
    id: np.ndarray
    name: object
    acronym: object
    rgb: np.uint8
    level: np.ndarray
    parent: np.ndarray


class BrainRegions(_BrainRegions):
    """
    ibllib.atlas.regions.BrainRegions(brainmap='Allen')
    The Allen atlas ids are kept intact but lateralized as follows: labels are duplicated
     and ids multiplied by -1, with the understanding that left hemisphere regions have negative
     ids.
    """
    def __init__(self):
        df_regions = pd.read_csv(FILE_REGIONS)
        # lateralize
        df_regions_left = df_regions.iloc[np.array(df_regions.id > 0), :].copy()
        df_regions_left['id'] = - df_regions_left['id']
        df_regions_left['parent_structure_id'] = - df_regions_left['parent_structure_id']
        df_regions_left['name'] = df_regions_left['name'].apply(lambda x: x + ' (left)')
        df_regions = pd.concat((df_regions, df_regions_left), axis=0)
        # converts colors to RGB uint8 array
        c = np.uint32(df_regions.color_hex_triplet.map(
            lambda x: int(x, 16) if isinstance(x, str) else 256 ** 3 - 1))
        c = np.flip(np.reshape(c.view(np.uint8), (df_regions.id.size, 4))[:, :3], 1)
        c[0, :] = 0  # set the void region to black
        # creates the BrainRegion instance
        super().__init__(id=df_regions.id.to_numpy(),
                         name=df_regions.name.to_numpy(),
                         acronym=df_regions.acronym.to_numpy(),
                         rgb=c,
                         level=df_regions.depth.to_numpy(),
                         parent=df_regions.parent_structure_id.to_numpy())
        # mappings are indices not ids: they range from 0 to n regions -1
        mappings = pd.read_parquet(FILE_MAPPINGS)
        self.mappings = {k: mappings[k].to_numpy() for k in mappings}

    def _compute_mappings(self):
        """
        Recomputes the mapping indices for all mappings
        This is left mainly as a reference for adding future mappings as this take a few seconds
        to execute. In production,we use the MAPPING_FILES npz to avoid recompuing at each \
        instantiation
        """
        beryl = np.load(FILE_BERYL)
        cosmos = np.load(FILE_COSMOS)
        self.mappings = {
            'Allen': self._mapping_from_regions_list(np.unique(np.abs(self.id)), lateralize=False),
            'Allen-lr': np.arange(self.id.size),
            'Beryl': self._mapping_from_regions_list(beryl, lateralize=False),
            'Beryl-lr': self._mapping_from_regions_list(beryl, lateralize=True),
            'Cosmos': self._mapping_from_regions_list(cosmos, lateralize=False),
            'Cosmos-lr': self._mapping_from_regions_list(cosmos, lateralize=True),
        }
        pd.DataFrame(self.mappings).to_parquet(FILE_MAPPINGS)

    def get(self, ids) -> Bunch:
        """
        Get a bunch of the name/id
        """
        uid, uind = np.unique(ids, return_inverse=True)
        a, iself, _ = np.intersect1d(self.id, uid, assume_unique=False, return_indices=True)
        b = Bunch()
        for k in self.__dataclass_fields__.keys():
            b[k] = self.__getattribute__(k)[iself[uind]]
        return b

    def _navigate_tree(self, ids, direction='down'):
        """
        Private method to navigate the tree and get all related objects either up or down
        :param ids:
        :param direction:
        :return: Bunch
        """
        indices = ismember(self.id, ids)[0]
        count = np.sum(indices)
        while True:
            if direction == 'down':
                indices |= ismember(self.parent, self.id[indices])[0]
            elif direction == 'up':
                indices |= ismember(self.id, self.parent[indices])[0]
            else:
                raise ValueError("direction should be either 'up' or 'down'")
            if count == np.sum(indices):  # last iteration didn't find any match
                break
            else:
                count = np.sum(indices)
        return self.get(self.id[indices])

    def descendants(self, ids):
        """
        Get descendants from one or an array of ids
        :param ids: np.array or scalar representing the region primary key
        :return: Bunch
        """
        return self._navigate_tree(ids, direction='down')

    def ancestors(self, ids):
        """
        Get ancestors from one or an array of ids
        :param ids: np.array or scalar representing the region primary key
        :return: Bunch
        """
        return self._navigate_tree(ids, direction='up')

    def leaves(self):
        """
        Get all regions that do not have children
        :return:
        """
        leaves = np.setxor1d(self.id, self.parent)
        return self.get(np.int64(leaves[~np.isnan(leaves)]))

    def _mapping_from_regions_list(self, new_map, lateralize=False):
        """
        From a vector of regions id, creates a mapping such as
        newids = self.mapping
        :param new_map: np.array: vector of regions id
        """
        I_ROOT = 1
        I_VOID = 0
        # to lateralize we make sure all regions are represented in + and -
        new_map = np.unique(np.r_[-new_map, new_map])
        assert np.all(np.isin(new_map, self.id)), \
            "All mapping ids should be represented in the Allen ids"
        # with the lateralization, self.id may have duplicate values so ismember is necessary
        iid, inm = ismember(self.id, new_map)
        iid = np.where(iid)[0]
        mapind = np.zeros_like(self.id) + I_ROOT  # non assigned regions are root
        # TO DO should root be lateralised?
        mapind[iid] = iid  # regions present in the list have the same index
        # Starting by the higher up levels in the hierarchy, assign all descendants to the mapping
        for i in np.argsort(self.level[iid]):
            descendants = self.descendants(self.id[iid[i]]).id
            _, idesc, _ = np.intersect1d(self.id, descendants, return_indices=True)
            mapind[idesc] = iid[i]
        mapind[0] = I_VOID  # void stays void
        # to delateralize the regions, assign the positive index to all mapind elements
        if lateralize is False:
            _, iregion = ismember(np.abs(self.id), self.id)
            mapind = mapind[iregion]
        return mapind

    def remap(self, region_ids, source_map='Allen', target_map='Beryl'):
        """
        Remap atlas regions ids from source map to target map
        :param region_ids: atlas ids to map
        :param source_map: map name which original region_ids are in
        :param target_map: map name onto which to map
        :return:
        """
        _, inds = ismember(region_ids, self.id[self.mappings[source_map]])
        return self.id[self.mappings[target_map][inds]]

    def remap_id2id(self, atlas_ids, source_map='Allen', target_map='Beryl'):
        """
        Remap atlas ids from source map to target map
        :param region_ids: atlas ids to map
        :param source_map: map name which original atlas_ids are in
        :param target_map: map name onto which to map
        :return:
        """
        _, inds = ismember(atlas_ids, self.id[self.mappings[source_map]])
        return self.id[self.mappings[target_map][inds]]

    def remap_acro2acro(self, acronyms, source_map='Allen', target_map='Beryl'):
        """
        Remap atlas acronyms from source map to target map
        :param acronyms: acronyms to map
        :param source_map: map name which original acronyms are in
        :param target_map: map name onto which to map
        :return:
        """
        _, inds = ismember(acronyms, self.acronym[self.mappings[source_map]])
        return self.acronym[self.mappings[target_map][inds]]

    def remap_acro2id(self, acronyms, source_map='Allen', target_map='Beryl'):
        """
        Remap atlas acronyms from source map to target map and convert to atlas ids
        :param acronyms: acronyms to map
        :param source_map: map name which original acronyms are in
        :param target_map: map name onto which to map
        :return:
        """
        _, inds = ismember(acronyms, self.acronym[self.mappings[source_map]])
        return self.id[self.mappings[target_map][inds]]

    def remap_id2acro(self, atlas_ids, source_map='Allen', target_map='Beryl'):
        """
        Remap atlas ids from source map to target map and convert to acronyms
        :param atlas_ids: atlas ids to map
        :param source_map: map name which original region_ids are in
        :param target_map: map name onto which to map
        :return:
        """
        _, inds = ismember(atlas_ids, self.id[self.mappings[source_map]])
        return self.acronym[self.mappings[target_map][inds]]


    def acronym2acronym(self, acronym, mapping='Allen'):
        inds = self._find_inds(acronym, self.acronym)
        return self.acronym[self.mappings[mapping]][inds]

    def acronym2atlasID(self, acronym, mapping='Allen', hemisphere=None):
        mapping = self._infer_mapping(mapping, hemisphere)
        inds = self._find_inds(acronym, self.acronym)
        #return self.id[self.mappings[mapping]][inds]
        return self.id[self.mappings[mapping]][self._filter_lr_acro(inds, mapping, hemisphere)]

    # need to think more carefully
    # Do we want to find all or just the first? All for sure
    def acronym2index(self, acronym, mapping='Allen', hemisphere=None):

        mapping = self._infer_mapping(mapping, hemisphere)
        index = dict()
        # make a dict of arrays for each acronym
        for acro in acronym:
            inds = self._find_inds(acro, self.acronym[self.mappings[mapping]])
            index[acro] = self._filter_lr_index(inds, hemisphere)

        return index

    # TODO check it works with list or with array
    def atlasID2acronym(self, atlas_id, mapping='Allen'):
        inds = self._find_inds(atlas_id, self.id, vtype='ids')
        return self.acronym[self.mappings[mapping]][inds]

    def atlasID2atlasID(self, atlas_id, mapping='Allen'):
        inds = self._find_inds(atlas_id, self.id, vtype='ids')
        return self.id[self.mappings[mapping]][inds]

    #def atlasID2index(self, atlas_id, mapping='Allen', hemisphere='both'):
    #    _, inds = ismember(atlas_id, self.id)
    #    return self._filter_lr(inds)

    #def index2acronym(self, index, mapping='Allen', hemisphere='both'):
    #    return np.where(br.acronym == acronym)[0]
#
    #def index2atlasID(self, acronym, mapping='Allen', hemisphere='both'):
    #    return np.where(br.acronym == acronym)[0]

    def _infer_mapping(self, mapping, hemisphere):
        if '-lr' in mapping:
            return mapping
        elif hemisphere is None:
            return mapping
        else:
            return mapping + '-lr'

    def _filter_lr_acro(self, values, mapping, hemisphere):
        if hemisphere == 'left':
            return values + 1327
        elif hemisphere == 'right':
            return values
        elif 'lr' in mapping:
            # we want left - negative values to be returned first
            return np.c_[values + 1327, values]
        else:
            return values

    def _filter_lr_index(self, values, hemisphere):
        if hemisphere == 'left':
            return values[:, 1]
        elif hemisphere == 'right':
            return values[:, 0]
        else:
            return np.fliplr(values)

    def _find_inds(self, values, all_values, vtype='acronym'):
        if not isinstance(values, list) and not isinstance(values, np.ndarray):
            values = np.array([values])
        #loc, inds = ismember(all_values, np.array(values))

        #inds = all_values[inds]
        #inds = np.where(loc)[0][np.argsort(inds)]
        loc, inds = ismember(np.array(values), all_values)

        return inds

        #if vtype == 'acronym':
        #    return inds.reshape(np.int16(inds.shape[0] / 2), 2)
        #else:
        #    return inds


    def _find_inds_multi(self, values, all_values):
        if not isinstance(values, list) and not isinstance(values, np.ndarray):
            values = np.array([values])
        loc, inds = ismember(all_values, np.array(values))
        inds = np.where(loc)[0]
        return inds.reshape(2, np.int16(inds.shape[0] / 2)).T


def regions_from_allen_csv():
    """
    Reads csv file containing the ALlen Ontology and instantiates a BrainRegions object
    :return: BrainRegions object
    """
    _logger.warning("ibllib.atlas.regions.regions_from_allen_csv() is deprecated. "
                    "Use BrainRegions() instead")
    return BrainRegions()

