# -*- coding: utf-8 -*-
# SyConn - Synaptic connectivity inference toolkit
#
# Copyright (c) 2016 - now
# Max-Planck-Institute for Medical Research, Heidelberg, Germany
# Authors: Sven Dorkenwald, Philipp Schubert, Jörgen Kornfeld

import copy
import gc
import socket
import time
from multiprocessing import Pool, Manager
from scipy import sparse
from shutil import copyfile
from sys import stdout
import networkx as nx
from axoness import majority_vote
from axoness import predict_axoness_from_nodes
from features import calc_prop_feat_dict
from learning_rfc import write_feat2csv
from spiness import assign_neck
from ..utils.datahandler import *
from ..utils.segmentationdataset import UltrastructuralDataset
from synapticity import parse_synfeature_from_node
from ..multi_proc.multi_proc_main import start_multiprocess
from knossos_utils import chunky
from knossos_utils.knossosdataset import KnossosDataset
from sklearn.externals import joblib
from knossos_utils.skeleton import Skeleton
from knossos_utils.skeleton import SkeletonNode
from knossos_utils.skeleton import from_skeleton_to_mergelist

try:
    from syconn.ray_casting.ray_casting_radius import ray_casting_radius
    ray_cast_avail = True
except ImportError:
    print("ray_casting_radius-module not imported")
    ray_cast_avail = False


class SkeletonMapper(object):
    """Class to handle mapping of cell objects (mitochondria, vesicle clouds,
    synaptic clefts) to tracings. Mapping parameters are saved as attributes.

    Attributes
    ----------
    soma : SkeletonAnnotation
        Soma tracing
    old_anno : SkeletonAnnotation
        original tracing where estimated cell radius is saved at each node
    anno : SkeletonAnnotation
        interpolated tracing skeleton for hull calculation
    mitos/vc/sj : segmentationDataset
        Dictionaries in which mapped cell objects are saved
    ix : int
        mapped skeleton id
    write_obj_voxel : bool
        write object voxel to kzip as binary file
    """

    def __init__(self, source, dh, ix=None, soma=None, context_range=6000):
        """
        Parameters
        ----------
        source: SkeletonAnnotation/str
            initial tracing object or path to .k.zip file
        dh: DataHandler
            DataHandler object
        ix : int
            index of tracings
        soma : SkeletonAnnotation
            if additional soma tracing is available
        context_range : int
            range for feature extraction of spiness and axoness
        """
        self.context_range = context_range
        self.scaling = arr(dh.scaling, dtype=np.int)
        self._mem_path = dh.mem_path
        self._nb_cpus = dh.nb_cpus
        self._cset_path = dh.cs_path
        self._myelin_ds_path = dh.myelin_ds_path
        init_anno = SkeletonAnnotation()
        init_anno.scaling = [9, 9, 20]
        init_anno.appendComment('soma')
        self.soma = init_anno
        self.mitos = None
        self.vc = None
        self.sj = None
        if type(source) is str:
            self.ix = re.findall('[^/]+$', source)[0][:-4]
            self._path = source
            self.old_anno = load_ordered_mapped_skeleton(source)[0]
            self.anno, self.soma = load_ordered_mapped_skeleton(source)[0, 4]
            obj_dicts = load_objpkl_from_kzip(source)
            self.mitos = obj_dicts[0]
            self.vc = obj_dicts[1]
            self.sj = obj_dicts[2]
        elif isinstance(source, SkeletonAnnotation):
            self._path = None
            self.ix = ix
            self.old_anno = source
            self.anno = copy.deepcopy(source)
            if soma is not None:
                self.soma = soma
        else:
            raise RuntimeError('Datatype not understood in __init__'
                               'of SkeletonMapper.')
        # init mapping parameters
        self.detect_outlier = True
        self.neighbor_radius = None
        self.nb_neighbors = None
        self.nb_rays = None
        self.nb_hull_vox = None
        self.nb_voting_neighbors = None
        self.annotation_method = 'hull'
        self.kd_radius = 1200
        self.thresh = 2.2
        self.filter_size = [0, 0, 0]
        self.write_obj_voxel = False
        self.obj_min_votes = {'mitos': 235, 'vc': 191, 'sj': 346}
        self.mapping_info = {'sj': {}, 'mitos': {}, 'vc': {}}
        self._cset = None
        # stores hull and radius estimation of each ray and node
        self._hull_coords = None
        self._hull_normals = None
        self._skel_radius = None
        if hasattr(self.old_anno, 'hull_coords'):
            self._hull_coords = self.old_anno.hull_coords
            self._hull_normals = self.old_anno.hull_normals
        # init skeleton nodes
        self._property_features = None
        self.property_feat_names = None
        self.anno.interpolate_nodes()
        if len(self.soma.getNodes()) != 0:
            self.merge_soma_tracing()
        self._create_nodes()

    @property
    def cset(self):
        if self._cset is None:
            self._cset = chunky.load_dataset(self._cset_path)
        return self._cset

    @property
    def hull_coords(self):
        """Scaled hull coordinates of skeleton membrane

        Returns
        -------
        np.array
            Coordinate each hull point
        """
        if self._hull_coords is None:
            self.hull_sampling(thresh=self.thresh,
                               detect_outlier=True,
                               nb_rays=20, nb_neighbors=20, neighbor_radius=220,
                               max_dist_mult=1.4)
        return self._hull_coords

    @property
    def hull_normals(self):
        """Normal for each hull point pointing outwards

        Returns
        -------
        np.array
            Normal vector of each hull point pointing outwards
        """
        if self._hull_normals is None:
            node_coords = arr(self.node_com)
            skel_tree = spatial.cKDTree(node_coords)
            hull_coords = self.hull_coords
            nearest_skel_nodes = arr(skel_tree.query(hull_coords, k=1)[1])
            nearest_coords = node_coords[nearest_skel_nodes]
            dir_vecs = hull_coords - nearest_coords
            hull_normals = dir_vecs * (1 / np.linalg.norm(dir_vecs,
                                                          axis=1))[:, None]
            if len(self.soma.getNodes()) != 0:
                soma_nodes = self.soma.getNodes()
                if len(soma_nodes) != 0:
                    soma_coords_pure = arr([node.getCoordinate_scaled() for node
                                            in soma_nodes])
                    soma_node_ixs = arr(skel_tree.query(soma_coords_pure, k=1)[1])
                    com_soma = np.mean(soma_coords_pure, axis=0)
                    dist, nn_ixs = skel_tree.query(hull_coords, k=1)
                    for ii, ix in enumerate(nn_ixs):
                        if ix in soma_node_ixs:
                            hull_normals[ii] = hull_coords[ii] - com_soma
            self._hull_normals = hull_normals
        return self._hull_normals

    @property
    def skel_radius(self):
        """Radius of membrane at each skeleton node

        Returns
        -------
        np.array
            cell radius at self.nodes
        """
        if self._skel_radius is None:
            self.hull_sampling(thresh=self.thresh,
                               detect_outlier=True,
                               nb_rays=20, nb_neighbors=20, neighbor_radius=220,
                               max_dist_mult=1.4)
        return self._skel_radius

    def _create_nodes(self):
        """Creates sorted node list and corresponding ID- and coordinate-list.
        Enables fast access to node information in same ordering.
        Setter for node_com, node_ids and nodes attribute.
        """
        coords = []
        ids = []
        self.nodes = []
        graph = su.annotation_to_nx_graph(self.anno)
        for i, node in enumerate(nx.dfs_preorder_nodes(graph)):
            coords.append(node.getCoordinate()*self.scaling)
            ids.append(node.ID)
            # contains mapped objects
            node.objects = {'vc': [], 'mitos': [], 'sj': []}
            self.nodes.append(node)
        self.node_com = arr(coords, dtype=np.int)
        self.node_ids = ids
        self.anno.nodes = set(self.nodes)

    def merge_soma_tracing(self):
        # print "Merging soma (%d nodes) with original annotation." % \
        #       (len(self.soma.getNodes()))
        self.soma.interpolate_nodes(150)
        self.anno = su.merge_annotations(self.anno, self.soma)

    def annotate_objects(self, dh, radius=1200, method='hull', thresh=2.2,
                         filter_size=(0, 0, 0), nb_neighbors=20,
                         nb_hull_vox=500, neighbor_radius=220,
                         detect_outlier=True, nb_rays=20,
                         nb_voting_neighbors=100, max_dist_mult=1.4):
        """Creates self.object with annotated objects as UltrastructuralDataset,
        where object is in {mitos, vc, sj}

        Parameters
        ----------
        dh : DataHandler
            object containing SegmentationDataObjects mitos, vc, sj
        radius : int
            Radius in nm. Single integer if integer radius is for all
            objects the same. If list of three integer stick to ordering
            [mitos, vc, sj].
        method : str
            Either 'kd' for fix radius or 'hull'/'supervoxel' if
            membrane is available.
        thresh : float
            Denotes the factor which is multiplied with the maximum
            membrane probability. The resulting value is used as threshold after
            which the membrane is assumed to be existant.
        filter_size : int
            List of integer for each object [mitos, vc, sj]
        nb_neighbors : int
            minimum number of neighbors needed during
            outlier detection for a single hull point to survive.
        nb_hull_vox : int
            Number of object hull voxels which are used to
            estimate spatial proximity to skeleton (inside or outside).
        neighbor_radius : int
            Radius (nm) of ball in which to look for supporting
            hull voxels. Used during outlier detection.
        detect_outlier : bool
            use outlier-detection if True.
        nb_rays : int
            Number of rays send at each skeleton node
            (multiplied by a factor of 5). Defines the angle between two rays
            (=360 / nb_rays) in the orthogonal plane.
        nb_voting_neighbors : int
            Number votes of skeleton hull voxels (membrane
            representation) for object-mapping. Used for vc and mitos during
            geometrical position estimation of object nodes.
        max_dist_mult : float
            Multiplier for radius to estimate maximal distance of hull points
            to source node.
        """
        start = time.time()
        if radius == 0:
            return
        if np.isscalar(radius):
            radius = [radius] * 3
        self.kd_radius = radius
        self.kd_radius = radius
        self.filter_size = arr(filter_size)
        self.annotation_method = method
        self.thresh = thresh
        self.detect_outlier = detect_outlier
        self.neighbor_radius = neighbor_radius
        self.nb_neighbors = nb_neighbors
        self.nb_rays = nb_rays
        self.nb_hull_vox = nb_hull_vox
        self.nb_voting_neighbors = nb_voting_neighbors
        if method == 'hull' and (self._hull_coords is None):
            self.hull_sampling(thresh, nb_rays, nb_neighbors,
                               neighbor_radius, detect_outlier, max_dist_mult)

        if dh.mitos is not None:
            # initialize segmentationDatasets for mapped objects
            self.mitos = UltrastructuralDataset(dh.mitos.type, dh.mitos._rel_path_home,
                                             dh.mitos._path_to_chunk_dataset_head)
            # do the mapping
            node_ids = self.annotate_object(dh.mitos, radius[0], method, "mitos")
            self.mitos._node_ids = node_id2key(dh.mitos, node_ids, filter_size[0])
            nb_obj_found = len(set([element for sublist in self.mitos._node_ids for
                                    element in sublist]))
            # print "[%s] Found %d %s using size filter %d" % \
            #       (self.ix, nb_obj_found, 'mitos', filter_size[0])
            # store annotated segmentation objects
            for i in range(len(self.nodes)):
                mito_keys = self.mitos._node_ids[i]
                for k in mito_keys:
                    self.nodes[i].objects['mitos'] = self.nodes[i].objects['mitos']\
                                                     + [dh.mitos.object_dict[k]]
                    self.mitos.object_dict[k] = dh.mitos.object_dict[k]
        else:
            # print "Skipped mito-mapping."
            pass
        # same for vc
        if dh.vc is not None:
            self.vc = UltrastructuralDataset(dh.vc.type, dh.vc._rel_path_home,
                                          dh.vc._path_to_chunk_dataset_head)
            self.vc._node_ids = node_id2key(dh.vc, self.annotate_object(
                dh.vc, radius[1], method, "vc"), filter_size[1])
            nb_obj_found = len(set([element for sublist in self.vc._node_ids for
                                    element in sublist]))
            # print "[%s] Found %d %s using size filter %d" % \
            #       (self.ix, nb_obj_found, 'vc', filter_size[1])
            for i in range(len(self.nodes)):
                vc_keys = self.vc._node_ids[i]
                for k in vc_keys:
                    self.nodes[i].objects['vc'] = self.nodes[i].objects['vc'] + \
                                                  [dh.vc.object_dict[k]]
                    self.vc.object_dict[k] = dh.vc.object_dict[k]
        else:
            # print "Skipped vc-mapping."
            pass
        # and sj
        if dh.sj is not None:
            self.sj = UltrastructuralDataset(dh.sj.type, dh.sj._rel_path_home,
                                          dh.sj._path_to_chunk_dataset_head)
            self.sj._node_ids = node_id2key(dh.sj, self.annotate_object(
                dh.sj, radius[2], method, "sj"), filter_size[2])
            nb_obj_found = len(set([element for sublist in self.sj._node_ids for
                                    element in sublist]))
            # print "[%s] Found %d %s using size filter %d" % \
            #       (self.ix, nb_obj_found, 'sj', filter_size[2])
            for i in range(len(self.nodes)):
                sj_keys = self.sj._node_ids[i]
                for k in sj_keys:
                    self.nodes[i].objects['sj'] = self.nodes[i].objects['sj'] + \
                                                  [dh.sj.object_dict[k]]
                    self.sj.object_dict[k] = dh.sj.object_dict[k]
        else:
            # print "Skipped sj-mapping."
            pass
        if self._myelin_ds_path is not None:
            self.calc_myelinisation()
        # print "--- Skeleton #%s fully annotated after %0.2f seconds with" \
        #       " '%s'-criterion" % (self.ix, time.time() - start,
        #                            self.annotation_method)

    def annotate_object(self, objects, radius, method, objtype):
        """Redirects mapping task to desired method-function

        Parameters
        ----------
        objects: UltrastructuralDataset
        radius: int
            Radius of kd-tree in units of nm.
        method: str
            either 'hull', 'kd' or 'supervoxel'
        objtype : string
            characterising object type

        Returns
        -------
        list
            mapped object ID's
        """
        if method == 'hull':
            node_ids = self._annotate_with_hull(objects, radius, objtype)
        elif method == 'supervoxel':
            node_ids = self._annotate_with_supervoxels(objects, radius, objtype)
        elif method == 'gt_sampling':
            node_ids = self._annotate_with_kdtree_gt_sampling(objects, radius)
        else:
            node_ids = self._annotate_with_kdtree(objects, radius)
        return list(node_ids)

    def _annotate_with_kdtree_gt_sampling(self, data, radius):
        """Annotates objects to node if its representative coordinate (data) is
         within radius and samples dependent on the distance of each object
         such that the objecet distance distribution to its nearest node is
         nearly uniform (assume isotrope distribution at the beginning, i.e.
         ~ r**2).

        Parameters
        ----------
        data : UltrastructuralDataset
            Dictionary of cell objects
        radius : int
            Cell radius at tracing nodes (in nm)

        Returns
        -------
        list
            annotated objects per node, i.e. list of lists
        """
        # print "Applying kd-tree with radius %s to %d nodes and %d objects" % \
        #       (radius, len(self.node_com), len(data.rep_coords))
        coords = arr(data.rep_coords) * self.scaling
        tree = spatial.cKDTree(coords)
        # Get objects within constant radius for all nodes
        assert radius > 0, "Choose positive radius!"
        annotation_ids = tree.query_ball_point(self.node_com, radius)
        dists = []
        for k, sublist in enumerate(annotation_ids):
            node_coord = self.node_com[k]
            dist_sub = np.linalg.norm(node_coord-coords[sublist], axis=1)
            dists.append(dist_sub)
        nb_objects = len(set([element for sublist in annotation_ids for element
                                             in sublist]))
        annotation_ids = arr([element for sublist in annotation_ids for element
                                                     in sublist])
        dists = arr([element for sublist in dists for element in sublist])
        set_of_anno_ids = list(set(annotation_ids))
        # print "Found %d objects before sampling." % nb_objects
        if nb_objects <= 400:
            return [[]]*(len(self.nodes)-1)+[set_of_anno_ids]

        todo_list = [[list(set_of_anno_ids[i::self._nb_cpus]),
                      annotation_ids, dists] for i in xrange(self._nb_cpus)]
        pool = Pool(processes=self._nb_cpus)
        res = pool.map(helper_samllest_dist, todo_list)
        pool.close()
        pool.join()
        final_ids = arr([ix for sub_list in res for ix in sub_list[0]])
        final_dists = arr([dist for sub_list in res for dist in sub_list[1]])
        max_dist = np.max(final_dists)
        a = -0.95 * max_dist**2
        w_func = lambda x: a*x**(-2)+1
        weights = w_func(final_dists)
        normalization = np.sum(weights)
        weights /= normalization
        cum_weights = np.cumsum(weights)
        sample_ixs = []
        cnt = 0
        while len(sample_ixs) < 400:
            cnt += 1
            rand_nb = np.random.rand(1)
            # find first occurance of entry with higher value than random number
            sample = np.argmax(cum_weights > rand_nb)
            if not sample in sample_ixs:
                sample_ixs.append(sample)
            if cnt > 50000:
                break
        return [[]]*(len(self.nodes)-1)+[list(final_ids[sample_ixs])]

    def _annotate_with_kdtree(self, data, radius):
        """Annotates objects to node if its representative coordinate (data) is
         within radius.

        Parameters
        ----------
        data : UltrastructuralDataset
            Dictioanry of cell objects
        radius: int
            radii list (in nm)

        Returns
        -------
        list of list of UltrastructuralDatasetObjects
            List with annotated objects per node, i.e. list of lists
        """
        # print "Applying kd-tree with radius %s to %d nodes and %d objects" % \
        #       (radius, len(self.node_com), len(data.rep_coords))
        coords = arr(data.rep_coords) * self.scaling
        tree = spatial.cKDTree(coords)
        annotation_ids = []
        # Get objects within constant radius for all nodes
        assert radius > 0, "Choose positive radius!"
        for coord in self.node_com:
            annotation_ids.append(tree.query_ball_point(coord, radius))
        nb_objects = len(set([element for sublist in annotation_ids for element
                                             in sublist]))
        return annotation_ids

    def _annotate_with_supervoxels(self, data, radius, objtype):
        """Annotates objects to skeleton if sufficient randomly selected
        object hull voxels are within supervoxels of this skeleton.
        radius.

        Parameters
        ----------
        data : UltrastructuralDataset
            Dictioanry of cell objects
        radius: int
            radii list (in nm)
        objtype : str
            Cell object type (sj, vc, mito)

        Returns
        -------
        list of list of UltrastructuralDatasetObjects
            List with annotated objects per node, i.e. list of lists
        """
        nb_hull_vox = self.nb_hull_vox
        red_ids = self._annotate_with_kdtree(data, radius)
        red_ids = list(set([ix for sublist in red_ids for ix in sublist]))
        keys = arr(data.ids)[red_ids]
        curr_objects = [data.object_dict[key] for key in keys]
        pool = Pool(processes=self._nb_cpus)
        obj_voxel_coords = pool.map(helper_get_voxels, curr_objects)
        pool.close()
        pool.join()
        cset = self.cset
        obj_ids = []
        rand_voxels = []
        for i, key in enumerate(keys):
            curr_voxels = obj_voxel_coords[i]
            curr_obj_id = curr_objects[i].obj_id
            rand_ixs = np.random.randint(len(curr_voxels), size=nb_hull_vox)
            rand_voxels += curr_voxels[rand_ixs].tolist()
            obj_ids += [curr_obj_id] * nb_hull_vox
        mergelist_path = '/home/pschuber/data/gt/nml_obj/'+str(self.ix)
        mapped_obj_ids = arr(from_skeleton_to_mergelist(
            cset, self.anno, 'watershed_150_20_10_3_unique', 'labels',
            rand_voxels, obj_ids, nb_processes=self._nb_cpus,
            mergelist_path=mergelist_path))
        annotation_ids_new = []
        min_votes = self.obj_min_votes[objtype]
        for i in range(len(obj_voxel_coords)):
            ix = curr_objects[i].obj_id
            inside_votes = np.sum(mapped_obj_ids[mapped_obj_ids[:, 0]==ix][:,1])
            if inside_votes >= min_votes:
                annotation_ids_new.append(red_ids[i])
            self.mapping_info[objtype][ix] = inside_votes
        return [[]]*(len(self.nodes)-1) + [annotation_ids_new]

    def _annotate_with_hull(self, data, radius, objtype):
        """
        Calculates a membrane representation via ray-castings. Each ray ends as
        a point after reaching a certain threshold. The resulting point cloud
        is used to determine in- and outlier coordinates of object hull voxels.
        If sufficient voxels are inside the cloud, the corresponding object
        is mapped to the skeleton.

        Parameters
        ----------
        data : UltrastructuralDataset
            Dictioanry of cell objects
        radius: int
            radii list (in nm)
        objtype : str
            Cell object type (sj, vc, mito)

        Returns
        -------
        list of list of UltrastructuralDatasetObjects
            List with annotated objects per node, i.e. list of lists
        """
        sjtrue = (objtype == 'sj')
        max_sj_dist = 125.
        nb_voting_neighbors = self.nb_voting_neighbors
        nb_hull_vox = self.nb_hull_vox
        # print "Annotating with hull criterion. Using %d voting neighbors and" \
        #       " %d hull voxel." % (nb_voting_neighbors, nb_hull_vox)
        red_ids = self._annotate_with_kdtree(data, radius=radius)
        red_ids = list(set([id for sublist in red_ids for id in sublist]))
        points = self.hull_coords
        if len(points) == 0:
            return [[]] * len(self.nodes)
        tree = spatial.cKDTree(points)
        def check_hull_normals(obj_coord, hull_coords, dir_vecs):
            if not sjtrue:
                obj_coord = obj_coord[None, :]
                left_side = np.inner(obj_coord, dir_vecs)
                right_side = np.sum(dir_vecs * hull_coords, axis=1)
                sign = np.sign(left_side - right_side)
                return np.sum(sign) < 0
            else:
                n_hullnodes_dists, n_hullnodes_ids = tree.query(obj_coord, k=20)
                mean_dists = np.mean(n_hullnodes_dists)
                return mean_dists < max_sj_dist

        # here annotation_ids_new contains only one node.
        keys = arr(data.ids)[red_ids]
        curr_objects = [data.object_dict[key] for key in keys]
        nb_cpus = max(cpu_count() / 2 - 2, 1)
        pool = Pool(processes=nb_cpus)
        curr_object_voxels = pool.map(helper_get_voxels, curr_objects)
        pool.close()
        pool.join()
        annotation_ids_new = []
        min_votes = self.obj_min_votes[objtype]
        # print "Mapping objects '%s' using %d min. votes while asking %s obj. " \
        #       "hull voxel and using %d skeleton hull voxel to decide if in or" \
        #       " out." % (objtype, min_votes, nb_hull_vox, nb_voting_neighbors)

        for i in range(len(curr_object_voxels)):
            curr_obj_id = curr_objects[i].obj_id
            curr_voxels = curr_object_voxels[i]
            rand_ixs = np.random.randint(len(curr_voxels), size=nb_hull_vox)
            rand_voxels = curr_voxels[rand_ixs] * self.scaling
            _, skel_hull_ixs = tree.query(rand_voxels, k=nb_voting_neighbors)
            is_in_hull = 0
            for ii, voxel in enumerate(rand_voxels):
                vx_near_cellixs = skel_hull_ixs[ii]
                is_in_hull += check_hull_normals(voxel, points[vx_near_cellixs],
                                            self.hull_normals[vx_near_cellixs])
            if is_in_hull >= min_votes:
                annotation_ids_new.append(red_ids[i])
            self.mapping_info[objtype][curr_obj_id] = is_in_hull
        return [[]]*(len(self.nodes)-1)+[annotation_ids_new]

    def hull_sampling(self, thresh=2.2, nb_rays=20, nb_neighbors=20,
                      neighbor_radius=220, detect_outlier=True,
                      max_dist_mult=1.4):
        """ Calculates hull of tracing

        Parameters
        ----------
        thresh : float
            factor of maximum occurring prediction value
            after which membrane is triggered active.
        nb_rays : int
            Number of rays send at each skeleton node
            (multiplied by a factor of 5). Defines the angle between two rays
            (=360 / nb_rays) in the orthogonal plane.
        nb_neighbors : int
            minimum number of neighbors needed during
            outlier detection for a single hull point to survive.
        neighbor_radius : int
            Radius of ball in which to look for supporting
            hull voxels. Used during outlier detection.
        detect_outlier : bool
            use outlier-detection if True.
        max_dist_mult : float
            Multiplier for radius to generate maximal distance of hull points
            to source node.

        Returns
        -------
        numpy.array
            Average radius per node in (9,9,20) corrected units estimated by
            rays propagated through Membrane prediction until threshold reached.
        """
        # print "Creating hull using scaling %s and threshold %0.2f with" \
        #       " outlier-detetion=%s" % (self.scaling, thresh*255.0,
        #                                 str(detect_outlier))
        mem_path = self._mem_path
        assert mem_path is not None, "Path to barrier must be given!"
        kd = KnossosDataset()
        kd.initialize_from_knossos_path(mem_path)
        used_node_ix = []
        coms = []
        mem_pos = np.array([0, 0, 0], dtype=np.int)
        mem_shape = kd.boundary
        # compute orthogonal plane to linear interpolated skeleton at each com
        orth_plane, skel_interp = get_orth_plane(self.node_com)
        # test and rewrite node positions of skeleton_data
        for i, com in enumerate(self.node_com):
            com = (np.array(com) - mem_pos) / self.scaling
            smaller_zero = np.any(com < 0)
            out_of_mem = np.any([com[k] > mem_shape[k] for k in range(3)])
            if not smaller_zero or out_of_mem:
                coms.append(com)
                used_node_ix.append(i)
        used_node_ix = arr(used_node_ix)
        coms = arr(coms)
        nb_nodes2proc = len(coms)
        # print "Computing radii and point cloud for %d of %d nodes." % \
        #       (nb_nodes2proc, len(self.node_com))
        # print "Total bounding box from %s to %s" % (str(np.min(coms, axis=0)),
        #                                             str(np.max(coms, axis=0)))
        assert (len(orth_plane) == len(skel_interp)) and \
               (len(skel_interp) == len(coms))
        # Find necessary bounding boxes containing nodes and index to get
        # corresponding orth. plane and interp.
        boxes = []
        box = [coms[0]]
        node_attr = []
        ix = used_node_ix[0]
        # check if current node is end node
        current_node = self.nodes[ix]
        nb_edges = len(self.anno.getNodeReverseEdges(current_node)) + \
                   len(self.anno.getNodeEdges(current_node))
        # store properties of nodes
        node_attr.append((skel_interp[ix], orth_plane[ix], ix, nb_edges<2))
        for i in range(1, len(coms)):
            node_box_min = np.min(box+[coms[i]], axis=0)
            node_box_max = np.max(box+[coms[i]], axis=0)
            vol = np.prod(node_box_max - node_box_min)
            if vol > 0.5e7:
                boxes.append((arr(box), node_attr))
                box = []
                node_attr = []
            box.append(coms[i])
            ix = used_node_ix[i]
            current_node = self.nodes[ix]
            nb_edges = len(self.anno.getNodeReverseEdges(current_node)) + \
                       len(self.anno.getNodeEdges(current_node))
            node_attr.append((skel_interp[ix], orth_plane[ix], ix, nb_edges<2))
        boxes.append((arr(box), node_attr))
        # print "Found %d different boxes." % len(boxes)
        # print "Using %d cpus." % self._nb_cpus
        pool = Pool(processes=self._nb_cpus)
        m = Manager()
        q = m.Queue()
        result = pool.map_async(get_radii_hull, [(box, q, self.scaling,
                                mem_path, nb_rays, thresh, max_dist_mult)
                                for box in boxes])
        outputs = result.get()
        pool.close()
        pool.join()
        # print "\nFinished radius estimation and hull representation."
        ixs = []
        radii = []
        hull_list = []
        vals = []
        for cnt, el in enumerate(outputs):
            radii += list(el[0])
            ixs += list(el[1])
            hull_list += list(el[2])
            vals += list(el[3])

        # sort to match self.node_ids ordering
        ixs = arr(ixs)
        ixs_sorted = np.argsort(ixs)
        radii_sorted = arr(radii)[ixs_sorted]
        # check result
        if len(ixs) != len(self.node_com):
            raise RuntimeError("Tracing nodes during hull mapping missing!")
        elif not (ixs[ixs_sorted] == np.arange(len(self.node_com))).all():
            raise RuntimeError("Original tracing node indices differ from "
                               "returned indices in membrane radius result.")
        coord_list = []
        for i, node in enumerate(self.nodes):
            node.setDataElem("radius", radii_sorted[i])
            coord_list.append(node.getCoordinate())
            node.ID = np.int(node.ID)
        big_skel_tree = spatial.cKDTree(coord_list)
        for node in self.old_anno.getNodes():
            ix_node = big_skel_tree.query(node.getCoordinate(), 1)[1]
            node.setDataElem("radius", np.max((radii_sorted[ix_node], 1.)))
        self.anno.nodes = set(self.nodes)
        try:
            hull_coords = arr([pt for sub in hull_list for pt in sub])*self.scaling
        except ValueError:
            hull_coords = np.zeros((0, 3))
        hull_coords = np.nan_to_num(hull_coords).astype(np.float32)
        if detect_outlier:
            hull_coords_ix = outlier_detection(hull_coords, nb_neighbors,
                                               neighbor_radius)
            hull_coords = hull_coords[hull_coords_ix]
        self._hull_coords = hull_coords
        self._skel_radius = radii_sorted

    def calc_myelinisation(self):
        """Calculates myelinisation at each node and writes it to
        node.data["myelin_pred"]
        """
        assert self._myelin_ds_path is not None, "Myelin dataset not found."
        test_box = (10, 10, 5)
        true_thresh = 100.
        j0126_myelin_inside_ds = KnossosDataset()
        j0126_myelin_inside_ds.initialize_from_knossos_path(self._myelin_ds_path)

        for n in self.old_anno.getNodes():
            myelin_b = '0'
            test_vol = j0126_myelin_inside_ds.from_raw_cubes_to_matrix(test_box,
                                    n.getCoordinate(), show_progress=False)
            if np.mean(test_vol) > true_thresh:
                myelin_b = '1'
                n.data["myelin_pred"] = 1
            else:
                n.data["myelin_pred"] = 0
            node_comment = n.getComment()
            ax_ix = node_comment.find('myelin')
            if ax_ix == -1:
                n.appendComment('myelin'+myelin_b)
            else:
                help_list = list(node_comment)
                help_list[ax_ix+5] = myelin_b
                n.setComment("".join(help_list))
        majority_vote(self.old_anno, property='myelin', max_dist=2000)

    @property
    def property_features(self):
        """Getter of property features, calculates axoness/spiness features
        if necessary

        Returns
        -------
        np.array, bool
            property features, if spiness feature are given
        """
        if self._property_features is None:
            self._property_features, self.property_feat_names, \
            self.spiness_given = calc_prop_feat_dict(self, self.context_range)
        return self._property_features

    def predict_property(self, rf, prop, max_neck2endpoint_dist=3000,
                         max_head2endpoint_dist=600):
        """Predict property (axoness, spiness) of tracings

        Parameters
        ----------
        rf: RandomForestClassifier
        prop: str
            property name
        max_neck2endpoint_dist: int
        max_head2endpoint_dist: int
        """
        property_feature = self.property_features[prop][:, 1:]
        if prop == 'axoness' and not self.spiness_given:
            raise RuntimeError("Spiness feature were not given "
                               "during axoness prediction!")
        # print "Predicting %s using %d features." % \
        #       (prop, property_feature.shape[1])
        proba = rf.predict_proba(property_feature)
        pred = rf.predict(property_feature)
        node_ids = self.property_features[prop][:, 0]
        for k, node_id in enumerate(node_ids):
            node = self.old_anno.getNodeByID(node_id)
            if prop == 'spiness' and 'axoness_pred' in node.data.keys():
                if int(node.data['axoness_pred']) != 0:
                    continue
            node_comment = node.getComment()
            ax_ix = node_comment.find(prop)
            node_pred = int(pred[k])
            if ax_ix == -1:
                node.appendComment(prop+'%d' % node_pred)
            else:
                help_list = list(node_comment)
                help_list[ax_ix+7] = str(node_pred)
                node.setComment("".join(help_list))
            for ii in range(proba.shape[1]):
                node.setDataElem(prop+'_proba%d' % ii, proba[k, ii])
            node.setDataElem(prop+'_pred', node_pred)
            node.setDataElem('branch_dist', property_feature[k, -1])
            node.setDataElem('end_dist', property_feature[k, -2])
        # if prop == 'axoness':
        #     majority_vote(self.old_anno, 'axoness', 25000)
        #     pass
        if prop == 'spiness':
            assign_neck(self.old_anno,
                        max_head2endpoint_dist=max_head2endpoint_dist,
                        max_neck2endpoint_dist=max_neck2endpoint_dist)

    def write2pkl(self, path):
        """Writes MappedSkeleton object to .pkl file. Path is extracted from
        dh._datapath and MappedSkeleton ID.

        Parameters
        ----------
        path: str
            Path to kzip destination
        """
        if os.path.isfile(path):
            copyfile(path, path[:-4]+'_old.pkl')
            # print ".pkl file already existed, moved old one to %s." %\
            #       (path[:-4]+'_old.pkl')
        with open(path, 'wb') as output:
           pickle.dump(self, output, -1)
           # print "Skeleton %s saved successfully at %s." % (self.ix, path)

    def write2kzip(self, path):
        """Writes interpolated skeleton (and annotated objects) to nml at path.
        If self.write_obj_voxel flag is True a .txt file containing all object
        voxel with id is written in k.zip

        Parameters
        ----------
        path: str
            Path to kzip destination
        """
        object_skel = Skeleton()
        obj_dict = {0: 'mitos', 1: 'vc', 2: 'sj'}
        re_process_skels = []
        # store path to written files for kzip compression
        files = []
        # print 'Writing kzip to %s. Writing object voxels=%s' \
        #       % (path, str(self.write_obj_voxel))
        if '.k.zip' in path:
            path = path[:-5] + 'nml'
        elif '.zip' in path:
            path = path[:-3] + 'nml'
        for k, objects in enumerate([self.mitos, self.vc, self.sj]):
            object_annotation = SkeletonAnnotation()
            object_annotation.scaling = self.scaling
            object_annotation.appendComment(obj_dict[k])
            if objects is None:
                continue
            object_voxel = []
            object_voxel_id = []
            if not np.all(arr(objects.sizes) >= self.filter_size[k]):
                print "Size filter does not work properly!"
                re_process_skels.append(id)
            for key in list(arr(objects.object_dict.keys())):
                obj = objects.object_dict[key]
                curr_obj_id = np.int(obj.obj_id)
                map_info = self.mapping_info[obj_dict[k]][curr_obj_id]
                node = SkeletonNode().from_scratch(
                    object_annotation, obj.rep_coord[0], obj.rep_coord[1],
                    obj.rep_coord[2], radius=(obj.size/4./np.pi*3)**(1/3.))
                node.setPureComment(obj_dict[k]+'-'+str(curr_obj_id)+'_mi'+
                                    str(map_info))
                object_annotation.addNode(node)
                if self.write_obj_voxel:
                    try:
                        coords_to_add = list(obj.hull_voxels*self.scaling)
                    except IOError, e:
                        # print "Could not find hull vx of object %s" % str(key)
                        # print e
                        warnings.warn("Could not find hull voxel. "
                                      "Aborting %s." % path,
                                      DeprecationWarning)
                        continue
                    object_voxel += coords_to_add
                    object_voxel_id += [np.int(obj.obj_id)] * len(coords_to_add)
            if self.write_obj_voxel:
                obj_hull_path = path[:-4] + '_' + obj_dict[k] + '.txt'
                obj_hull2text(arr(object_voxel_id), arr(object_voxel), obj_hull_path)
                files.append(obj_hull_path)
                files.append(obj_hull_path[:-4]+'_id.txt')
            obj_pkl_path = path[:-4] + '_' + obj_dict[k] + '.pkl'
            write_obj2pkl(objects, obj_pkl_path)
            files.append(obj_pkl_path)
            object_skel.add_annotation(object_annotation)
        self.old_anno.setComment("skeleton")
        object_skel.add_annotation(self.old_anno)
        if self.soma is not None:
            object_skel.add_annotation(self.soma)
        object_skel.toNml(path)
        files.append(path)
        if self._hull_coords is not None:
            hull_path = path[:-3] + 'xyz'
            hull2text(self.hull_coords, self.hull_normals, hull_path)
            files.append(hull_path)
        kzip_path = path[:-3] + "k.zip"
        try:
            for prop, prop_feat in self.property_features.iteritems():
                feat_path = path[:-4] + '_%s_feat.csv' % prop
                write_feat2csv(feat_path, prop_feat, self.property_feat_names[prop])
                files.append(feat_path)
        except IOError:
            pass
        for path_to_file in files:
            write_data2kzip(kzip_path, path_to_file)
        # print "Mapped skeleton %s saved successfully at %s." % (self.ix,
        #                                                         kzip_path)

    def get_plot_obj(self):
        """Extracts coordinates from annotated SegmentationObjects

        Returns
        -------
        np.array
            object-voxels for each object
        """
        assert self.annotation_method != None, "Objects not initialized!"
        voxel_list = []
        for objects in [self.mitos, self.vc, self.sj]:
            voxel_list1 = []
            for key in objects.object_dict.keys():
                voxels = objects.object_dict[key].voxels
                if np.ndim(voxels) == 1:
                    voxels = voxels[None, :]
                voxel_list1.append(voxels)
            voxel_list.append(voxel_list1)
        mito = arr([element for sublist in voxel_list[0] for element in sublist],
                        dtype=np.uint32) * (self.scaling / 10)
        vc = arr([element for sublist in voxel_list[1] for element in sublist],
                        dtype=np.uint32) * (self.scaling / 10)
        sj = arr([element for sublist in voxel_list[2] for element in sublist],
                        dtype=np.uint32) * (self.scaling / 10)
        return mito, vc, sj


def node_id2key(segdataobject, node_ids, filter_size):
    """
    Maps list indices in node_ids to keys of SegmentationObjects. Filters
    objects bigger than filter_size.

    Parameters
    ----------
    :param segdataobject: UltrastructuralDataset of object type currently processed
    :param node_ids: List of list containing annotated object ids for each node
    :param filter_size: int minimum number of voxels of object

    Returns
    -------
    list
        objects keys
    """

    for node in node_ids:
        for obj in node:
            if segdataobject.sizes[obj] < filter_size:
                node[node.index(obj)] = -1
            else:
                key = segdataobject.ids[obj]
                node[node.index(obj)] = key
    node_ids = [filter(lambda a: a != -1, node) for node in node_ids]
    return node_ids


def outlier_detection(point_list, min_num_neigh, radius):
    """Finds hull outlier using point density criterion

    Parameters
    ----------
    point_list: list
        List of coordinates
    min_num_neigh: int
        Minimum number of neighbors, s.t. hull-point survives.
    radius: int
        Radius in nm to look for neighbors

    Returns
    -------
    numpy.array
        Cleaned point cloud
    """
    if len(point_list) == 0:
        return np.ones((len(point_list), )).astype(np.bool)
    # print "Starting outlier detection."
    if np.array(point_list).ndim != 2:
        points = np.array([point for sublist in point_list for point in sublist])
    else:
        points = np.array(point_list)
    tree = spatial.cKDTree(points)
    nb_points = float(len(points))
    # print "Old #points:\t%d" % nb_points
    new_points = np.ones((len(points), )).astype(np.bool)
    for ii, coord in enumerate(points):
        neighbors = tree.query_ball_point(coord, radius)
        num_neighbors = len(neighbors)
        new_points[ii] = num_neighbors>=min_num_neigh
    # print "Found %d outlier." % np.sum(~new_points)
    return np.array(new_points)


def get_radii_hull(args):
    if not ray_cast_avail:
        raise RuntimeError("ray_casting_radius-module needed for this")
    """Wrapper-function for point cloud extraction from membrane prediction.
    Gets a bounding box with nodes, loads the membrane prediction for these
    and then calculates the radius and hull at each skeleton node.
    """
    # node attribute contains skel_interpolation, orthogonal plane and
    # bool if node is end node
    box, node_attr = args[0]
    q = args[1]
    scaling = args[2]
    mem_path = args[3]
    nb_rays = args[4]
    thresh_factor = args[5]
    max_dist_mult = args[6]
    kd = KnossosDataset()
    kd.initialize_from_knossos_path(mem_path)
    mem_shape = kd.boundary
    ray_buffer = arr([2000, 2000, 2000])/scaling
    prop_offset = np.max([np.min(box, axis=0) - ray_buffer,
                          [0,0,0]], axis=0).astype(np.int)
    prop_size = np.min([np.max(box, axis=0) + ray_buffer, mem_shape],
                       axis=0) - prop_offset
    assert np.prod(prop_size) < 10e9, "Bounding box too big!"
    mem = kd.from_raw_cubes_to_matrix(prop_size.astype(np.int32),
                                      prop_offset.astype(np.int32),
                                      show_progress=False)
    # thresholding membrane
    mem[mem <= 0.4*mem.max()] = 0
    mem = mem.astype(np.uint8)
    threshold = mem.max() * thresh_factor
    # iterate over every node
    avg_radius_list = []
    all_points = []
    ids = []
    val_list = []
    todo_list = zip(list(box), [nb_rays] * len(box), list(node_attr))
    for el in todo_list:
        radius, ix, membrane_points, vals = ray_casting_radius(
            el[0], el[1], el[2][0], el[2][1], el[2][2],
            scaling, threshold, prop_offset, mem, el[2][3], max_dist_mult)
        all_points.append(arr(membrane_points, dtype=np.float32))
        avg_radius_list.append(radius)
        ids.append(ix)
        val_list.append(vals)
    q.put(ids)
    del mem
    return avg_radius_list, ids, all_points, val_list


def read_pair_cs(pair_path):
    """Helper function to collect pairwise contact site information. Extracts
    axoness prediction.

    Parameters
    ----------
    pair_path : str
        path to pairwise contact site kzip

    Returns
    -------
    SkeletonAnnotation
        annotation object without contact site hull voxel
    """
    pairwise_anno = su.loadj0126NML(pair_path)[0]
    predict_axoness_from_nodes(pairwise_anno)
    new_anno = SkeletonAnnotation()
    new_anno.setComment(pairwise_anno.getComment())
    for node in list(pairwise_anno.getNodes()):
        n_comment = node.getComment()
        if '_hull' in n_comment:
            continue
        new_anno.addNode(node)
    return new_anno


def prepare_syns_btw_annos(pairwise_paths, dest_path, max_hull_dist=60,
                           concom_dist=300):
    """
    Checks pairwise for contact sites between annotation objects found at paths
    in nml_list. Adds sj, vc and nearest skeleton nodes to found contact sites.
    Writes 'contact_sites.nml' to nml-path containing contact sites of all
    nml's.

    Parameters
    ----------
    pairwise_paths : list of str
        List of pairwise paths to nml's
    dest_path : str
        Path to directory where to store result of
     synapse mapping
    max_hull_dist : float
        maximum distance between skeletons in nm
    concom_dist : float
        Maximum distance of connected components (nm)
    """
    sname = socket.gethostname()
    if sname[:6] in ['soma01', 'soma02', 'soma03', 'soma04', 'soma05']:
        nb_cpus = np.min((2, cpu_count()-1))
    else:
        nb_cpus = np.max([np.min((16, cpu_count()-1)), 1])
    params = [(a, b, max_hull_dist, concom_dist, dest_path) for a, b
              in pairwise_paths]
    _ = start_multiprocess(syn_btw_anno_pair, params, nb_cpus=nb_cpus)


def similarity_check(skel_a, skel_b):
    """If absolute number of identical nodes is bigger then certain threshold
    return similar.

    Parameters
    ----------
    skel_a : SkeletonAnnotation
        Skeleton a
    skel_b: SkeletonAnnotation
        Skeleton b

    Returns
    -------
    bool
        skel_a and skel_b are similar
    """
    a_coords = arr([node.getCoordinate() for node in skel_a.getNodes()]) * \
               skel_a.scaling
    a_coords_sample = a_coords[np.random.randint(0, len(a_coords), 100)]
    b_coords = arr([node.getCoordinate() for node in skel_b.getNodes()]) * \
                skel_b.scaling
    b_tree = spatial.cKDTree(b_coords)
    a_near = b_tree.query_ball_point(a_coords_sample, 1)
    nb_equal = len([id for sublist in a_near for id in sublist])
    similar = nb_equal > 10
    return similar


def similarity_check_star(params):
    """Helper function"""
    skel1 = load_ordered_mapped_skeleton(params[0])[0]
    skel2 = load_ordered_mapped_skeleton(params[1])[0]
    similar = similarity_check(skel1, skel2)
    return similar, params


def syn_btw_anno_pair(params):
    """
    Get synapse information between two mapped annotation objects. Details are
    written to pairwise nml (all contact sites between pairs contained) and
    to nml for each contact site.

    Parameters
    ----------
    params : list
        [path_a, path_b, max_hull_dist, concom_dist]
    path_a : str
        path to mapped annotation object
    path_b : str
        path to mapped annotation object
    max_hull_dist : float
        maximum distance between skeletons (nm)
    concom_dist : float
        maximum distance of connected components (nm)
    """
    path_a, path_b, max_hull_dist, concom_dist, dest_path = params
    vx_overlap_dist = 80
    max_vc_dist = 80
    max_sj_dist = 40
    min_cs_area = 0.05 * 1e6
    # try:
    a = load_anno_list([path_a], load_mitos=False)[0]
    sj_dict = load_objpkl_from_kzip(path_a)[2].object_dict
    b = load_anno_list([path_b], load_mitos=False)[0]
    id2skel = lambda x: str(a[0].filename) if np.int(x) == 0 else\
        str(b[0].filename)
    sj_dict.update(load_objpkl_from_kzip(path_b)[2].object_dict)
    scaling = a[0].scaling
    match = re.search(r'iter_0_(\d+)', a[0].filename)
    if match:
        a[0].filename = match.group(1)
    match = re.search(r'iter_0_(\d+)', b[0].filename)
    if match:
        b[0].filename = match.group(1)
    annotation_name = 'skel_' + a[0].filename + '_' + b[0].filename
    # DO similarity check and skip combination if true
    if a[0].filename == b[0].filename:
        # print "\n Skipping nearly identical skeletons: %s and %s, " \
        #       "because of identical ID.\n " % (a[0].filename, b[0].filename)
        return None
    if similarity_check(a[0], b[0]):
        # print "\n Skipping nearly identical skeletons: %s and %s, " \
        #       "because of similarity check.\n" % (a[0].filename, b[0].filename)
        return None
    csites, csite_ids = cs_btw_annos(a[0], b[0], max_hull_dist, concom_dist)
    if len(csites) == 0:
        return None

    # save information about pairwise csites in one nml
    pairwise_anno = SkeletonAnnotation()
    pairwise_anno.appendComment(annotation_name)
    pairwise_anno.scaling = scaling

    # get sj_objects with hull voxels if available
    sj_nodes = list(a[3].getNodes()) + list(b[3].getNodes())
    sj_ids = []
    for node in sj_nodes:
        global_sj_id = np.int(re.findall('sj-(\d+)', node.getComment())[0])
        sj_ids.append(global_sj_id)
    sj_id_to_ix = {}
    for i, entry in enumerate(sj_ids):
        sj_id_to_ix[entry] = i
    if len(a[0].sj_hull_coords) != 0 or len(b[0].sj_hull_coords) != 0:
        sj_hull_voxel = np.concatenate((a[0].sj_hull_coords,
                                        b[0].sj_hull_coords), axis=0)
        sj_ids = np.concatenate((a[0].sj_hull_ids,
                                        b[0].sj_hull_ids), axis=0)
        sj_tree = spatial.cKDTree(sj_hull_voxel)
    else:
        sj_tree = None
    # get vc_objects with hull voxels if available
    vc_nodes = list(a[2].getNodes()) + list(b[2].getNodes())
    vc_ids = []
    for node in vc_nodes:
        global_vc_id = np.int(re.findall('vc-(\d+)', node.getComment())[0])
        vc_ids.append(global_vc_id)
    vc_id_to_ix = {}
    for i, entry in enumerate(vc_ids):
        vc_id_to_ix[entry] = i
    if len(a[0].vc_hull_coords) != 0 or len(b[0].vc_hull_coords) != 0:
        vc_hull_voxel = np.concatenate((a[0].vc_hull_coords,
                                        b[0].vc_hull_coords), axis=0)
        vc_ids = np.concatenate((a[0].vc_hull_ids,
                                        b[0].vc_hull_ids), axis=0)
        vc_tree = spatial.cKDTree(vc_hull_voxel)
    else:
        vc_tree = None

    # iterate over all contact sites between skeletons, calc skeleton
    # kd-tree in advance
    a_skel_node_list = [node for node in a[0].getNodes()]
    a_skel_node_coords = arr([node.getCoordinate() for node in
                           a_skel_node_list]) * arr(scaling)
    a_skel_node_tree = spatial.cKDTree(a_skel_node_coords)
    b_skel_node_list = [node for node in b[0].getNodes()]
    b_skel_node_coords = arr([node.getCoordinate() for node in
                           b_skel_node_list]) * arr(scaling)
    b_skel_node_tree = spatial.cKDTree(b_skel_node_coords)
    for i, csite in enumerate(csites):
        vc_bool = False
        sj_bool = False
        # save information about one contact site in extra nml dependent on
        # occuring vc and sj (four different categories)
        contact_site_name = annotation_name+'_cs%d' % (i+1)
        contact_site_anno = SkeletonAnnotation()
        contact_site_anno.scaling = scaling
        curr_csite_ids = arr(csite_ids[i])
        csite_name = 'cs'+str(i+1)+'_'
        csite_tree = spatial.cKDTree(csite)

        # get hull area
        csb_area = 0
        csa_area = 0
        csb_points = arr(csite)[curr_csite_ids]
        csa_points = arr(csite)[~curr_csite_ids]
        try:
            if np.sum(curr_csite_ids) > 3:
                csb_area = convex_hull_area(csb_points)
        except Exception, e:
            # print e
            # print "Could not calculate a_area!!!!"
            pass
        try:
            if np.sum(~curr_csite_ids) > 3:
                csa_area = convex_hull_area(csa_points)
        except Exception, e:
            # print e
            # print "Could not calculate b_area!!!!"
            pass
        for j, coord in enumerate(csite):
            coord_id = curr_csite_ids[j]
            node = SkeletonNode().from_scratch(
                contact_site_anno, coord[0]/scaling[0], coord[1]/scaling[1],
                coord[2]/scaling[2])
            node.setPureComment(csite_name + id2skel(coord_id) + '_hull')
            pairwise_anno.addNode(node)
        mean_cs_area = np.mean((csb_area, csa_area))
        if mean_cs_area < min_cs_area:
            # print "Skipping cs because of area:", mean_cs_area
            continue

        # get hull distance
        csa_tree = spatial.cKDTree(csa_points)
        dist, ixs = csa_tree.query(csb_points, 1)
        cs_dist = np.min(dist)
        # check vc and sj
        if sj_tree is not None:
            near_sj_ixs = sj_tree.query_ball_point(csite, max_sj_dist)
            near_sj_ids = list(set([sj_ids[id] for sublist in near_sj_ixs.tolist()
                                    for id in sublist]))
        else:
            near_sj_ids = []
        overlap = 0
        abs_ol = 0
        overlap_cs = 0
        overlap_area = 0
        overlap_coords = np.array([])
        for sj_id in near_sj_ids:
            sj_ix = sj_id_to_ix[sj_id]
            node = copy.copy(sj_nodes[sj_ix])
            curr_sj_voxel = np.array(sj_dict[sj_id].voxels) * scaling
            overlap_new, overlap_cs_new, overlap_area_new,\
                center_coord_new, overlap_coords_new = calc_overlap(
                    csite, curr_sj_voxel, vx_overlap_dist)
            abs_ol_new = overlap_new*len(curr_sj_voxel)
            old_comment = node.getComment()
            node.setPureComment(csite_name + 'relol%0.3f_absol%d' %
                                (overlap_new, abs_ol_new) + old_comment)
            contact_site_anno.addNode(node)
            pairwise_anno.addNode(node)
            if overlap_new > overlap:
                overlap = overlap_new
                abs_ol = abs_ol_new
                overlap_cs = overlap_cs_new
                overlap_area = overlap_area_new
                overlap_coords = overlap_coords_new
        if vc_tree is not None:
            near_vc_ixs = vc_tree.query_ball_point(csite, max_vc_dist)
            near_vc_ids = list(set([vc_ids[ix] for sublist in
                               near_vc_ixs.tolist() for ix in sublist]))
        else:
            near_vc_ids = []
        for vc_id in near_vc_ids:
            vc_ix = vc_id_to_ix[vc_id]
            node = copy.copy(vc_nodes[vc_ix])
            dist, nearest_ix = csite_tree.query(node.getCoordinate(), 1)
            nearest_id = curr_csite_ids[nearest_ix]
            old_comment = node.getComment()
            node.setPureComment(csite_name + id2skel(nearest_id) +
                                '_' + old_comment)
            contact_site_anno.addNode(node)
            pairwise_anno.addNode(node)

        # get center node (representative cs coordinate)
        cs_center = np.sum(csite, axis=0) / float(len(csite))
        cs_center_ix = csite_tree.query(cs_center)[1]
        cs_center = csite[cs_center_ix]
        node = SkeletonNode().from_scratch(contact_site_anno,
                                           cs_center[0]/scaling[0],
                                           cs_center[1]/scaling[1],
                                           cs_center[2]/scaling[2])
        comment = csite_name+'area%0.2f_dist%0.4f_center' % (mean_cs_area,
                                                             cs_dist)
        node.data['adj_skel1'] = a[0].filename
        node.data['adj_skel2'] = b[0].filename
        if len(near_vc_ids) > 0:
            vc_bool = True
            comment += '_vc'
            pairwise_anno.setComment(annotation_name+'_syn_candidate')
            contact_site_name += '_vc'
        if len(near_sj_ids) > 0:
            sj_bool = True
            comment += '_sj_relol%0.3f_absol%d_csrelol%0.3f_areaol%0.3f' % \
                       (overlap, abs_ol, overlap_cs, overlap_area)
            contact_site_name += '_sj'
            np.save(dest_path + '/overlap_vx/' + contact_site_name +
                    'ol_vx.npy', overlap_coords)
        node.data['syn_feat'] = np.array([cs_dist, mean_cs_area, overlap_area,
                                         overlap, abs_ol, overlap_cs])
        node.data['cs_dist'] = cs_dist
        node.data['mean_cs_area'] = mean_cs_area
        node.data['overlap_area'] = overlap_area
        node.data['overlap'] = overlap
        node.data['abs_ol'] = abs_ol
        node.data['overlap_cs'] = overlap_cs
        node.data['cs_name'] = contact_site_name
        node.setPureComment(comment)
        contact_site_anno.addNode(node)
        pairwise_anno.addNode(node)

        # get closest skeleton nodes
        dist, a_nearest_sn_ixs = a_skel_node_tree.query(cs_center, 2)
        a_source_node = a_skel_node_list[a_nearest_sn_ixs[0]]
        a_nn = max_nodes_in_path(a[0], a_source_node, 100)
        # get nearest node to source node of skeleton b and average radius
        a_source_node_nn = a_skel_node_list[a_nearest_sn_ixs[1]]
        mean_radius = np.mean([a_source_node.data['radius'],
                               a_source_node_nn.data['radius']])
        for j, node in enumerate(a_nn):
            if j == 0:
                comment = csite_name+a[0].filename+'_skelnode'+\
                          '_area %0.2f' % (csa_area)
                node.data['head_diameter'] = mean_radius * 2
                node.data['skel_id'] = int(a[0].filename)
            else:
                comment = csite_name+a[0].filename+'_skelnode%d' % j
            curr_node = copy.copy(node)
            curr_node.appendComment(comment)
            contact_site_anno.addNode(curr_node)
            pairwise_anno.addNode(curr_node)
        for j, node in enumerate(a_nn):
            try:
                target_node = list(a[0].getNodeEdges(node))[0]
                contact_site_anno.addEdge(node, target_node)
                pairwise_anno.addEdge(node, target_node)
            except (KeyError, IndexError):
                pass

        dist, b_nearest_sn_ixs = b_skel_node_tree.query(cs_center, 2)
        b_source_node = b_skel_node_list[b_nearest_sn_ixs[0]]
        b_nn = max_nodes_in_path(b[0], b_source_node, 100)
        # get nearest node to source node of skeleton b and average radius
        b_source_node_nn = b_skel_node_list[b_nearest_sn_ixs[1]]
        mean_radius = np.mean([b_source_node.data['radius'],
                               b_source_node_nn.data['radius']])
        for j, node in enumerate(b_nn):
            if j == 0:
                comment = csite_name+b[0].filename+'_skelnode'+'_area %0.2f'\
                                % (csb_area)
                node.data['head_diameter'] = mean_radius * 2
                node.data['skel_id'] = int(b[0].filename)
            else:
                comment = csite_name+b[0].filename+'_skelnode%d' % j
            curr_node = copy.copy(node)
            curr_node.appendComment(comment)
            contact_site_anno.addNode(curr_node)
            pairwise_anno.addNode(curr_node)
        for j, node in enumerate(b_nn):
            try:
                target_node = list(b[0].getNodeEdges(node))[0]
                contact_site_anno.addEdge(node, target_node)
                pairwise_anno.addEdge(node, target_node)
            except (KeyError, IndexError):
                pass
        contact_site_anno.setComment(contact_site_name)
        dummy_skel = Skeleton()
        dummy_skel.add_annotation(contact_site_anno)
        cs_destpath = dest_path
        if vc_bool and sj_bool:
            cs_destpath += 'cs_vc_sj/'
        elif vc_bool and not sj_bool:
            cs_destpath += 'cs_vc/'
        elif not vc_bool and sj_bool:
            cs_destpath += 'cs_sj/'
        elif not vc_bool and not sj_bool:
            cs_destpath += 'cs/'
        dummy_skel.toNml(cs_destpath+contact_site_name+'.nml')
    if len(pairwise_anno.getNodes()) == 0:
        # print "Did not found any node in annotation object."
        return None
    pairwise_anno.appendComment('%dcs' % len(csites))
    dummy_skel = Skeleton()
    dummy_skel.add_annotation(pairwise_anno)
    dummy_skel.toNml(dest_path+'pairwise/'+annotation_name+'.nml')
    del dummy_skel
    gc.collect()
    return 0


def max_nodes_in_path(anno, source_node, max_number):
    """Find specified number of nodes along skeleton from source node (BFS).

    Parameters
    ----------
    anno: SkeletonAnnotation
        tracing on which to search
    source_node: SkeletonNode
        Starting node
    max_number: int
        Maximum number of nodes

    Returns
    -------
    list of SkeletonNodes
        Tracing nodes up to certain distance from source node
    """
    skel_graph = su.annotation_to_nx_graph(anno)
    reachable_nodes = [source_node]
    for edge in nx.bfs_edges(skel_graph, source_node):
        next_node = edge[1]
        reachable_nodes.append(next_node)
        if len(reachable_nodes) >= max_number:
            break
    return reachable_nodes


def feature_valid_syns(cs_dir, only_sj=True, only_syn=True, all_contacts=False):
    """Returns the features of valid synapses predicted by synapse rfc

    Parameters
    ----------
    cs_dir : str
        Path to computed contact sites.
    only_sj : bool
        Return feature of all contact sites with mapped sj.
    only_syn : bool
        Returns feature only if synapse was predicted
    all_contacts : bool
        Use all contact sites for feature extraction

    Returns
    -------
    np.array (n x f), np.array (n x 1), np.array (n x 1)
        features, array of contact site IDS, boolean array of synapse prediction
    """
    clf_path = cs_dir + '/../models/rf_synapses/rfc_syn.pkl'
    cs_fpaths = []
    if only_sj:
        search_folder = ['cs_sj/', 'cs_vc_sj/']
    elif all_contacts:
        search_folder = ['cs_sj/', 'cs_vc_sj/', 'cs/', 'cs_vc/']
    else:
        search_folder = ['cs/', 'cs_vc/']
    sample_list_len = []
    for k, ending in enumerate(search_folder):
        curr_dir = cs_dir+ending
        curr_fpaths = get_filepaths_from_dir(curr_dir, ending='nml')
        cs_fpaths += curr_fpaths
        sample_list_len.append(len(curr_fpaths))
    if len(cs_fpaths) == 0:
        return np.zeros(0, ), np.zeros(0, ), np.zeros(0, ).astype(np.bool)
    nb_cpus = cpu_count()
    pool = Pool(processes=nb_cpus)
    m = Manager()
    q = m.Queue()
    params = [(sample, q) for sample in cs_fpaths]
    result = pool.map_async(readout_cs_info, params)
    res = result.get()
    pool.close()
    pool.join()
    res = arr(res)
    non_instances = arr([isinstance(el, np.ndarray) for el in res[:,0]])
    cs_infos = res[non_instances]
    features = arr([el.astype(np.float) for el in cs_infos[:,0]], dtype=np.float)
    if not only_sj or not only_syn or all_contacts:
        syn_pred = np.ones((len(features), ))
    else:
        rfc_syn = joblib.load(clf_path)
        syn_pred = rfc_syn.predict(features)
    axoness_info = cs_infos[:, 1]
    return features, axoness_info, syn_pred.astype(np.bool)


def readout_cs_info(args):
    """Helper function of feature_valid_syns

    Parameters
    ----------
    args: tuple
        path to file and queue

    Returns
    -------
    np.array, str
        synapse features, contact site ID
    """
    cspath, q = args
    feat = None
    if q is not None:
        q.put(1)
    cs = read_pair_cs(cspath)
    for node in cs.getNodes():
        if 'center' in node.getComment():
            feat = parse_synfeature_from_node(node)
            break
    return feat, cs.getComment()


def calc_syn_dict(features, axoness_info, get_all=False):
    """
    Creates dictionary of synapses. Keys are ids of pre cells and values are
    dictionaries of corresponding synapses with post cell ids.

    Parameters
    ----------
    features: np.array
        synapse feature
    axoness_info: np.array
        string containing axoness information of cells
    get_all : bool
        collect all contact sites

    Returns
    -------
    np.array, np.array, dict, np.array, np.array, dict
        synapse features, axoness information, connectivity,\
           post synaptic cell ids, synapse predictions, axoness
    """
    total_size = float(len(axoness_info))
    if total_size == 0:
        print "No synapse dict to create."
        return np.zeros(0, ), np.zeros(0,), {}, np.zeros(0, ), np.zeros(0, ), {}
    ax_ax_cnt = 0
    den_den_cnt = 0
    all_post_ids = []
    pre_dict = {}
    val_syn_ixs = []
    valid_syn_array = np.ones_like(features)
    axoness_dict = {}
    for k, ax_info in enumerate(axoness_info):
        stdout.write("\r%0.2f" % (k / total_size))
        stdout.flush()
        cell1, cell2 = re.findall('(\d+)axoness(\-?\d+)', ax_info)
        cs_nb = re.findall('cs(\d+)', ax_info)[0]
        cell_ids = arr([cell1[0], cell2[0]], dtype=np.int)
        cell_axoness = arr([cell1[1], cell2[1]], dtype=np.int)
        axoness_entry = {str(cell1[0]): cell1[1], str(cell2[0]): cell2[1]}
        axoness_dict[cs_nb + '_' + cell1[0] + '_' + cell2[0]] = axoness_entry
        if cell_axoness[0] == cell_axoness[1]:
            if cell_axoness[0] == 1:
                ax_ax_cnt += 1
            else:
                den_den_cnt += 1
                valid_syn_array[k] = 0
                if not get_all:
                    continue
        val_syn_ixs.append(k)
        pre_ix = np.argmax(cell_axoness)
        pre_id = cell_ids[pre_ix]
        if pre_ix == 0:
            post_ix = 1
        else:
            post_ix = 0
        post_id = cell_ids[post_ix]
        all_post_ids += [post_id]
        syn_dict = {}
        syn_dict['post_id'] = post_id
        syn_dict['post_axoness'] = cell_axoness[post_ix]
        syn_dict['cs_area'] = features[k, 1]
        syn_dict['sj_size_abs'] = features[k, 2]
        syn_dict['sj_size_rel'] = features[k, 3]
        if pre_id in pre_dict.keys():
            syns = pre_dict[pre_id]
            if post_id in syns.keys():
                syns[post_id]['cs_area'] += features[k, 1]
                syns[post_id]['sj_size_abs'] += features[k, 2]
            else:
                syns[post_id] = syn_dict
        else:
            syns = {}
            syns[post_id] = syn_dict
            pre_dict[pre_id] = syns
    return features[val_syn_ixs], axoness_info[val_syn_ixs], pre_dict,\
           all_post_ids, valid_syn_array, axoness_dict


def cs_btw_annos(anno_a, anno_b, max_hull_dist, concom_dist):
    """
    Computes contact sites between two annotation objects and returns hull
    points of both skeletons near contact site.

    Parameters
    ----------
    anno_a : SkeletonAnnotation
        Annotation object A
    anno_b : SkeletonAnnotation
        Annotation object B
    max_hull_dist : int
        Maximum distance between skeletons in nm
    concom_dist : int
        maximum distance of connected components (nm)

    Returns
    -------
    list
        List of hull coordinates for each contact site
    """
    hull_a = anno_a.hull_coords
    hull_b = anno_b.hull_coords
    if len(hull_a) == 0 or len(hull_b) == 0:
        # print "One skeleton hull is empty!! Skipping pair."
        return [], []
    tree_a = spatial.cKDTree(hull_a)
    tree_b = spatial.cKDTree(hull_b)
    contact_ids = tree_a.query_ball_tree(tree_b, max_hull_dist)
    num_neighbours = arr([len(sublist) for sublist in contact_ids])
    contact_coords_a = hull_a[num_neighbours>0]
    contact_ids_b = set([id for sublist in contact_ids for id in sublist])
    contact_coords_b = hull_b[list(contact_ids_b)]
    if contact_coords_a.ndim == 1:
        contact_coords_a = contact_coords_a[None, :]
    if contact_coords_b.ndim == 1:
        contact_coords_b = contact_coords_a[None, :]
    contact_coords = np.concatenate((contact_coords_a, contact_coords_b), axis=0)
    if contact_coords.shape[0] >= 0.95*(len(hull_a)+len(hull_b)):
        print "Found too many contact_coords (proportion of total hull voxel:" \
              "%0.3f) assuming similar skeleton comparison between skeleton" \
              "%s and %s. " \
              % (contact_coords.shape[0] / float(len(hull_a)+len(hull_b)),
                 anno_a.filename, anno_b.filename)
        return [], []
    if contact_coords.shape[0] == 0:
        return [], []
    pdists = spatial.distance.pdist(contact_coords)
    pdists[pdists > concom_dist] = 0
    pdists = sparse.csr_matrix(spatial.distance.squareform(pdists))
    nb_cc, labels = sparse.csgraph.connected_components(pdists)
    cs_list = []
    for label in set(labels):
        curr_label_ixs = labels == label
        cs_list.append(contact_coords[curr_label_ixs])
    # extract annotation ids
    tree_a_b = spatial.cKDTree(np.concatenate((hull_a, hull_b), axis=0))
    contact_site_coord_ids = []
    min_id_b = len(hull_a)
    for cs in cs_list:
        # map the contact site to each coordinate
        ids_temp = tree_a_b.query(cs, 1)[1]
        in_b = arr(ids_temp>=min_id_b, dtype=np.bool)
        contact_site_coord_ids.append(in_b)
    return cs_list, contact_site_coord_ids


def translate_dense_tracings():
    fpaths = get_filepaths_from_dir('/lustre/pschuber/dense_vol_tracings/source/')
    for p in fpaths:
        s = load_ordered_mapped_skeleton(p)[0]
        for n in s.getNodes():
            n.setCoordinate(n.getCoordinate()-np.array([3540, 4843, 2418]))
        file_name = os.path.basename(p)
        dummy_skel = Skeleton()
        dummy_skel.add_annotation(s)
        dummy_skel.to_kzip("/lustre/pschuber/SyConnDenseCube/tracings/" +
                           file_name)