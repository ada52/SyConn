from itertools import combinations
from syconn.utils.newskeleton import NewSkeleton
from multi_proc.multi_proc_main import __QSUB__, start_multiprocess, QSUB_script
from utils.datahandler import *
from contactsites import write_summaries
from processing.features import calc_prop_feat_dict
from processing.learning_rfc import write_feat2csv, load_rfcs
from processing.mapper import SkeletonMapper, prepare_syns_btw_annos
__author__ = 'pschuber'

# Multiprocessing parameter
nb_cpus = cpu_count()
# QSUB keyword arguments
kwargs = {'work_folder': "/home/pschuber/QSUB/", 'username': "pschuber",
          'python_path': "/home/pschuber/anaconda/bin/python",
          'path_to_scripts': "/home/pschuber/skeleton-analysis/Philipp/QSUB"}


def enrich_tracings_all(wd):
    """Enrich tracings from biological data

    Run :func: 'enrich_tracings' on available cluster nodes defined by
    somaqnodes or using multiprocessing.

    Parameters
    ----------
    wd : str
        Path to working directory
    """
    skel_dir = wd + '/tracings/'
    output_dir = wd + '/neurons/'
    anno_list = [os.path.join(skel_dir, f) for f in
                 next(os.walk(skel_dir))[2] if 'k.zip' in f]
    np.random.shuffle(anno_list)
    print "Found %d cell tracings." % len(anno_list)
    list_of_lists = [[anno_list[i::60], output_dir] for i in xrange(60)]
    if __QSUB__:
        QSUB_script(list_of_lists, 'skeleton_mapping', **kwargs)
    else:
        start_multiprocess(enrich_tracings, list_of_lists, nb_cpus=nb_cpus)


def enrich_tracings(wd, anno_list, map_objects=True, method='hull', radius=1200,
                    thresh=2.2, filter_size=(2786, 1594, 250),
                    create_hull=True, max_dist_mult=1.4, detect_outlier=True,
                    dh=None, overwrite=False, nb_neighbors=20,
                    nb_hull_vox=500, context_range=6000,
                    neighbor_radius=220, nb_rays=20, nb_voting_neighbors=100,
                    write_obj_voxel=True, output_dir=None):
    """Enrich tracings with biological data

    Enriches a list of paths (to tracings) using dataset in working
    directory. Writes enriched tracings to 'neuron' folder, or to
    ouput directory if specified.

    Parameters
    ----------
    wd : str
        Path to working directory
    anno_list : list
        paths to tracing .k.zip's
    map_objects : bool
        Map cell components to skeleton
    method : str
        Either 'kd' for fix radius or 'hull'/'supervoxel' if
        membrane is available.
    radius : int
        Radius in nm. Single integer if integer radius is for
        all objects the same. If list of three integer stick to ordering
        [mitos, p4, az].
    thresh : int
        Denotes the factor which is multiplied with the maximum
        membrane probability. The resulting value is used as threshold after
        which the membrane is assumed to be existant.
    nb_rays : int
        Number of rays send at each skeleton node
        (multiplied by a factor of 5). Defines the angle between two rays
        (=360 / nb_rays) in the orthogonal plane.
    neighbor_radius : int
        Radius of ball in which to look for supporting
        hull voxels. Used during outlier detection.
    nb_neighbors : int
        minimum number of neighbors needed during outlier detection
        for a single hull point to survive.
    filter_size : list, tuple of int
        minimum cell object sizes for each object (mitos, vesicle clouds,
        synaptic clefts)
    nb_hull_vox : int
        Number of object hull voxels which are used to
        estimate spatial proximity to skeleton.
    context_range : int
        Range of property features
    nb_voting_neighbors : int
        Number votes of skeleton hull voxels
        (membrane representation) for object-mapping.
    dh: DataHandler.__class__
        object containing SegmentationDataObjects
        mitos, p4, az
    create_hull : bool
        create skeleton membrane estimation
    max_dist_mult : float
        Multiplier for radius to generate maximal
        distance of hull points to source node.
    detect_outlier : bool
        Use outlier-detection if True
    overwrite : bool
        Overwrite existing .k.zip's of mapped skeletons
    output_dir : str
        Path to output directory, if None dh._data_path will be used.
    write_obj_voxel : bool
        write object voxel coordinates to .k.zip
    """
    rf_axoness_p = wd + '/models/rf_axoness/rf.pkl'
    rf_spiness_p = wd + '/models/rf_spiness/rf.pkl'
    if output_dir is None:
        if dh is None:
            raise RuntimeError("No output directory could be found.")
        else:
            output_dir = dh.data_path
    if not overwrite:
        existing_skel = [re.findall('[^/]+$', os.path.join(dp, f))[0] for
                         dp, dn, filenames in os.walk(output_dir)
                         for f in filenames if 'k.zip' in f]
    else:
        existing_skel = []
    if anno_list == [] and dh is not None:
        anno_list = [os.path.join(dp, f) for dp, dn, filenames in
                     os.walk(dh.skeleton_path) for f in filenames if
                     'consensus.nml' in f]
        anno_list += [os.path.join(dp, f) for dp, dn, filenames in
                      os.walk(dh.skeleton_path) for f in filenames if
                      'k.zip' in f]
    elif anno_list == [] and dh is None:
        raise RuntimeError("Aborting mapping procedure. Either DataHandler"
                           "with source path (dh.skeleton_path) or "
                           "nml_list must be given!")
    todo_skel = list(set([re.findall('[^/]+$', f)[0] for f in anno_list]) -
                     set(existing_skel))
    todo_skel = [f for f in anno_list if re.findall('[^/]+$', f)[0]
                 in todo_skel]
    if len(todo_skel) == 0:
        print "Nothing to do. Aborting."
        return
    if dh is None:
        dh = DataHandler(wd)
        dh.data_path = output_dir
    print "Found %d processed Skeletons. %d left. Writing result to %s. " \
          "Using %s barrier." % (len(existing_skel), len(todo_skel),
                                 dh.data_path, dh.mem_path)
    cnt = 0
    rfc_axoness, rfc_spiness = load_rfcs(rf_axoness_p, rf_spiness_p)
    for filepath in list(todo_skel)[::-1]:
        dh.skeletons = {}
        cnt += 1
        _list = load_ordered_mapped_skeleton(filepath)
        annotation = _list[0]
        soma = connect_soma_tracing(_list[4])
        if len(_list[4].getNodes()) != 0:
            print "Loaded soma of skeleton."
        try:
            ix = int(re.findall('.*?([\d]+)', filepath)[-3])
        except IndexError:
            ix = cnt
        path = dh.data_path + 'nml_obj/' + re.findall('[^/]+$', filepath)[0]
        skel = SkeletonMapper(annotation, dh.scaling, ix=ix, soma=soma,
                              context_range=context_range,
                              mem_path=dh.mem_path)
        skel.write_obj_voxel = write_obj_voxel
        if create_hull:
            skel.hull_sampling(detect_outlier=detect_outlier, thresh=thresh,
                               nb_neighbors=nb_neighbors,
                               neighbor_radius=neighbor_radius,
                               max_dist_mult=max_dist_mult)
        if map_objects:
            skel.annotate_objects(dh, radius, method, thresh,
                                  filter_size, nb_hull_vox=nb_hull_vox,
                                  nb_voting_neighbors=nb_voting_neighbors,
                                  nb_rays=nb_rays,
                                  nb_neighbors=nb_neighbors,
                                  neighbor_radius=neighbor_radius,
                                  max_dist_mult=max_dist_mult)
        print "Starting cell compartment prediction."
        if rfc_spiness is not None:
            skel.predict_property(rfc_spiness, 'spiness')
        skel._property_features = None
        if rfc_spiness is not None and rfc_axoness is not None:
            skel.predict_property(rfc_axoness, 'axoness')
        if not os.path.exists(dh.data_path):
            os.makedirs(dh.data_path)
        skel.write2kzip(path)


def remap_tracings_all(anno_list, dest_dir=None, recalc_prop_only=False,
                       method='hull', context_range=6000):
    """Remapping cell objects of all tracings

    Run :func: 'remap_tracings' on available cluster nodes defined by
    somaqnodes or using multiprocessing.

    anno_list : list of str
        Paths to skeleton nml / kzip files
    dest_dir : str
        Directory path to store mapped skeletons
    recalc_prop_only : bool
        Recalculate properties (spiness, axoness) only,
        without calculating hull
    method : str
        Method for object mapping procedure
    context_range : int
        Context range for property feature.
    """
    np.random.shuffle(anno_list)
    if dest_dir is not None and not os.path.isdir(dest_dir):
        os.makedirs(dest_dir)
    print "Found %d mapped Skeletons. Remapping with context range of %d" % \
          (len(anno_list), context_range)

    if __QSUB__:
        nb_processes = np.max((len(anno_list) / 3, 3))
        list_of_lists = [[anno_list[i::nb_processes], dest_dir,
                          recalc_prop_only, method, context_range]
                         for i in xrange(nb_processes)]
        QSUB_script(list_of_lists, 'skeleton_remapping', **kwargs)
    else:
        start_multiprocess(remap_tracings, [anno_list, dest_dir,
                           recalc_prop_only, method, context_range],
                           nb_cpus=nb_cpus)


def remap_tracings(wd, mapped_skel_paths, dh=None, method='hull', radius=1200,
                   thresh=2.2, filter_size=(2786, 1594, 250),
                   max_dist_mult=1.4,
                   nb_neighbors=20, nb_hull_vox=500,
                   neighbor_radius=220, nb_rays=20, nb_voting_neighbors=100,
                   output_dir=None, write_obj_voxel=True,
                   mito_min_votes=235, p4_min_votes=191, az_min_votes=346,
                   recalc_prop_only=True, context_range=6000):
    """ Remap objects in tracings with pre-calculated cell hull
     Remaps objects to skeleton without recalculating the hull.
     :param wd: Path to working directory
     :param mapped_skel_paths: Paths to tracings
     :param dh: DataHandler Object
     :param output_dir: str path to output dir. If none, use given path
     :param write_obj_voxel: bool write object voxel to k.zip
     :param az_min_votes: Best F2 score found by eval
     :param mito_min_votes: Best F1 score found by eval
     :param p4_min_votes: Best F2 score found by eval
     :param method: Either 'kd' for fix radius or 'hull'/'supervoxel' if
     membrane is available.
     :param radius: Radius in nm. Single integer if integer radius is for all
     objects the same. If list of three integer stick to ordering
     [mitos, p4, az].
     :param thresh: Denotes the factor which is multiplied with the maximum
     membrane probability. The resulting value is used as threshold after
     which the membrane is assumed to be existant.
     :param nb_rays: Integer, Number of rays send at each skeleton node
     (multiplied by a factor of 5). Defines the angle between two rays
     (=360 / nb_rays) in the orthogonal plane.
     :param neighbor_radius: Radius of ball in which to look for supporting
     hull voxels. Used during outlier detection.
     :param nb_neighbors: Integer, minimum number of neighbors needed during
     outlier detection for a single hull point to survive.
     :param filter_size: List of integer for each object [mitos, p4, az]
     :param nb_hull_vox: Number of object hull voxels which are used to
     estimate spatial proximity to skeleton.
     :param context_range: Range of property features
     :param nb_voting_neighbors: Number votes of skeleton hull voxels (membrane
     representation) for object-mapping.
     :param dh: DataHandler object containing SegmentationDataObjects
     mitos, p4, az
     :param max_dist_mult: float Multiplier for radius to generate maximal
     distance of hull points to source node.
     :param output_dir: Path to output directory, if None dh._data_path
     will be used.
     :param recalc_prop_only: Recalculate only features and prediction of
     properties (axoness, spiness)
     :param write_obj_voxel: write object voxel coordinates to kzip
     :type output_dir: str
     :type method: str
     :type radius: int
     :type thresh: float
     :type nb_rays: int
     :type neighbor_radius: int
     :type filter_size: list, tuple, ...
     :type nb_hull_vox: int
     :type nb_voting_neighbors: int
     :type dh: DataHandler.__class__
     :type max_dist_mult: float
     :type context_range: int
     :type recalc_prop_only: bool
     :type write_obj_voxel: bool

     :returns:
    """
    rf_axoness_p = wd + '/models/rf_axoness/rf.pkl'
    rf_spiness_p = wd + '/models/rf_spiness/rf.pkl'
    rfc_axoness, rfc_spiness = load_rfcs(rf_axoness_p, rf_spiness_p)
    cnt = 0
    for skel_path in mapped_skel_paths:
        cnt += 1
        # get first element in list and only skeletonAnnotation
        mapped_skel_old, mito, p4, az, soma = \
            load_anno_list([skel_path], load_mitos=False)[0]
        if output_dir is not None:
            if not os.path.exists(output_dir):
                print "Couldn't find output directory. Creating", output_dir
                os.makedirs(output_dir)
            for fpath in mapped_skel_paths:
                shutil.copyfile(fpath, output_dir + os.path.split(fpath)[1])
            path = output_dir + re.findall('[^/]+$', skel_path)[0]
        else:
            path = skel_path
        try:
            ix = int(re.findall('.*?([\d]+)', skel_path)[-3])
        except IndexError:
            ix = cnt
        print "Remapping skeleton at %s and writing result to %s.\n" \
              "Using context range of %d and method '%s'" % (skel_path, path,
                                                             context_range,
                                                             method)
        new_skel = SkeletonMapper(mapped_skel_old, mapped_skel_old.scaling,
                                  ix=ix, soma=soma)
        if recalc_prop_only:
            print "--- Recalculating properties only ---"
            new_skel.old_anno.filename = skel_path
        else:
            if dh is None:
                dh = DataHandler(wd)
            new_skel.obj_min_votes['mitos'] = mito_min_votes
            new_skel.obj_min_votes['p4'] = p4_min_votes
            new_skel.obj_min_votes['az'] = az_min_votes
            new_skel.write_obj_voxel = write_obj_voxel
            new_skel.annotate_objects(
                dh, radius, method, thresh, filter_size,
                nb_hull_vox=nb_hull_vox,
                nb_voting_neighbors=nb_voting_neighbors, nb_rays=nb_rays,
                nb_neighbors=nb_neighbors, neighbor_radius=neighbor_radius,
                max_dist_mult=max_dist_mult)
        if rfc_spiness is not None:
            new_skel._property_features, new_skel.property_feat_names = \
                calc_prop_feat_dict(new_skel, context_range)
            new_skel.predict_property(rfc_spiness, 'spiness')
            new_skel._property_features = None
        if rfc_spiness is not None and rfc_axoness is not None:
            new_skel.predict_property(rfc_axoness, 'axoness')
        if not recalc_prop_only:
            new_skel.write2kzip(path)
        else:
            dummy_skel = NewSkeleton()
            dummy_skel.add_annotation(new_skel.old_anno)
            dummy_skel.add_annotation(mito)
            dummy_skel.add_annotation(p4)
            dummy_skel.add_annotation(az)
            if soma is not None:
                dummy_skel.add_annotation(soma)
            dummy_skel.toNml(path[:-5] + 'nml')
            write_data2kzip(path, path[:-5] + 'nml')
            write_feat2csv(path + 'axoness_feat.csv',
                           new_skel.property_features['axoness'])
            write_feat2csv(path + 'spiness_feat.csv',
                           new_skel.property_features['spiness'])
            write_data2kzip(path, path + 'axoness_feat.csv')
            write_data2kzip(path, path + 'spiness_feat.csv')
        print "Remapped skeleton %s successfully." % path


def detect_synapses(wd):
    """Detect synapses between tracings
     Detects contact sites between enriched tracings and writes contact site
     summary and synapse summary to working directory.

    Parameters
    ----------
    wd : str Path to working directory
    """
    nml_list = get_filepaths_from_dir(wd + '/neurons/')
    cs_path = wd + '/contact_sites/'
    for ending in ['', 'cs', 'cs_p4', 'cs_az', 'cs_p4_az', 'pairwise',
                   'overlap_vx']:
        if not os.path.exists(cs_path+ending):
            os.makedirs(cs_path+ending)
    anno_permutations = list(combinations(nml_list, 2))
    if __QSUB__:
        list_of_lists = [[list(anno_permutations[i::300]), cs_path]
                         for i in xrange(300)]
        QSUB_script(list_of_lists, 'synapse_mapping', **kwargs)
    else:
        start_multiprocess(prepare_syns_btw_annos, [anno_permutations, cs_path],
                           nb_cpus=nb_cpus)
    write_summaries(cs_path)
