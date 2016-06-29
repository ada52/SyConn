import networkx as nx
import copy
import os
import numpy as np
import re
from multiprocessing import Pool, Manager, cpu_count
from sys import stdout
import time
from numpy import array as arr
from sklearn.externals import joblib
try:
    from NewSkeleton import annotationUtils as au
except:
    import annotationUtils as au
try:
    from NewSkeleton.NewSkeletonUtils import annotation_from_nodes
except:
    from NewSkeletonUtils import annotation_from_nodes
from heraca.utils.datahandler import get_filepaths_from_dir, \
    load_ordered_mapped_skeleton, get_skelID_from_path
from NewSkeleton import NewSkeleton, SkeletonAnnotation
from learning_rfc import cell_classification, save_train_clf, load_csv2feat
from features import assign_property2node, majority_vote,\
    update_property_feat_kzip, morphology_feature

__author__ = 'philipp'


def save_axoness_clf(gt_path='/lustre/pschuber/gt_axoness/',
                             clf_used='rf'):
    """
    Save axoness clf specified by clf_used to gt_directory.
    :param gt_path: str to directory of axoness ground truthsvnm
    :param clf_used: 'rf' or 'svm'
    """
    dendrite_features, axon_features, soma_feature, \
    skelnode_gt = load_axon_gt(gt_path)
    X_cells = arr(dendrite_features + axon_features + soma_feature)
    X_train = arr([item for sublist in X_cells for item in sublist])
    y_train = arr([item for sublist in skelnode_gt for item in sublist])
    x, y = load_axon_gt_cell()
    X_train = np.concatenate((X_train, x), axis=0)
    y_train = np.concatenate((y_train, y))
    save_train_clf(X_train, y_train, clf_used, gt_path)


def load_axon_gt_cell(recompute=False):
    anno_path = '/lustre/pschuber/gt_axoness/nml_obj/' \
                'axoness_gt_190.k.zip'
    if not os.path.isfile(anno_path+'.npy') or recompute:
        skel = load_ordered_mapped_skeleton(anno_path)[0]

        morph_feat, spine_feat, node_feat_ids = morphology_feature(anno_path)
        node_feats = np.concatenate((morph_feat, spine_feat), axis=1)
        skel_nodes = skel.getNodes()
        x = np.zeros((len(skel_nodes), 24))
        y = np.zeros(len(skel_nodes))
        for ii, node in enumerate(skel_nodes):
            node_id = node.getID()
            x[ii] = node_feats[node_feat_ids == node_id]
            y[ii] = int(re.findall('gt_ax(\d+)', node.getComment())[0])
        np.save(anno_path+'_x.npy', x)
        np.save(anno_path+'_y.npy', y)
    else:
        x = np.load(anno_path+'_x.npy')
        y = np.load(anno_path+'_y.npy')
    return x, y


def load_axon_gt(gt_path):
    gt_axon_files = get_filepaths_from_dir(gt_path+'axon/nml_obj/')
    gt_dendrite_files = get_filepaths_from_dir(gt_path+'dendrite/nml_obj/')
    gt_soma_files = get_filepaths_from_dir(gt_path+'soma/nml_obj/')
    axon_features = []
    dendrite_features = []
    soma_features = []
    skelnode_gt = []
    for dendrite_fname in gt_dendrite_files:
        input, header = load_csv2feat(dendrite_fname)
        curr_feat = input[:, 1:]
        dendrite_features.append(curr_feat)
        skelnode_gt.append([0]*curr_feat.shape[0])
    print "Computed dendrite feature from groundtruth."
    for axon_fname in gt_axon_files:
        input, header = load_csv2feat(axon_fname)
        curr_feat = input[:, 1:]
        axon_features.append(curr_feat)
        skelnode_gt.append([1]*curr_feat.shape[0])
    print "Computed axon feature from groundtruth."
    for soma_fname in gt_soma_files:
        input, header = load_csv2feat(soma_fname)
        curr_feat = input[:, 1:]
        soma_features.append(curr_feat)
        skelnode_gt.append([2]*curr_feat.shape[0])
    print "Computed soma feature from groundtruth."
    skelnode_gt = arr(skelnode_gt)
    return dendrite_features, axon_features, soma_features, skelnode_gt


def predict_axoness_mappedskel(fname_skel=[], recompute_feat=False):
    nb_cpus = cpu_count()
    pool = Pool(processes=nb_cpus)
    m = Manager()
    q = m.Queue()
    params = [(path, q, recompute_feat) for path in fname_skel]
    # result = pool.map_async(predict_axoness_of_single_mappedskel, params)
    result = map(predict_axoness_of_single_mappedskel, params)
    # monitor loop
    while True:
        if result.ready():
            break
        else:
            size = float(q.qsize())
            stdout.write("\r%0.2f" % (size / len(params)))
            stdout.flush()
            time.sleep(4)
    res = result.get()
    pool.close()
    pool.join()


def predict_axoness_of_single_mappedskel(args):
    path, q, recompute_feat = args
    anno, mitos, p4, az = load_ordered_mapped_skeleton(path)
    rfc_axoness = joblib.load('/lustre/pschuber/gt_axoness/rfc/rfc_axoness.pkl')
    if recompute_feat:
        update_property_feat_kzip(path)
    print "Load feature from file."
    input, header = load_csv2feat(path)
    axoness_feat = input[:, 1:]
    node_ids = input[:, 0].astype(np.int64)
    proba = rfc_axoness.predict_proba(axoness_feat)
    # TODO bug in newskeleton! correct to fix it like that?
    anno_node_ids = [node.getID() for node in anno.getNodes()]
    assert len(node_ids) == len(anno_node_ids), 'Length of stored features and'\
                                                'anno nodes differ!'
    diff = np.abs(np.min(node_ids) - np.min(anno_node_ids))
    print "Difference between node ids and saved node IDS:", diff
    for k, node_id in enumerate(node_ids):
        node = anno.getNodeByID(node_id + diff)
        node_comment = node.getComment()
        ax_ix = node_comment.find('axoness')
        pred = np.argmax(proba[k])
        if ax_ix == -1:
            node.appendComment('axoness%d' % pred)
        else:
            help_list = list(node_comment)
            help_list[ax_ix+7] = str(pred)
            node.setComment("".join(help_list))
        for ii in range(len(proba[k])):
            node.setDataElem('axoness_proba%d' % ii, proba[k, ii])
    #majority_vote(anno, 'axoness', 6000)
    majority_vote(anno, 'axoness', 3000)
    grow_out_soma(anno)
    majority_processes(anno)
    dummy_skel = NewSkeleton()
    dummy_skel.add_annotation(anno)
    dummy_skel.add_annotation(mitos)
    dummy_skel.add_annotation(p4)
    dummy_skel.add_annotation(az)
    dummy_skel.to_kzip(path[:-6] + '_smoothed_process_majority.k.zip')
    if q is not None:
        q.put(1)


def predict_axoness_from_node_comments(anno):
    """
    Exctracts axoness prediction from nodes for given contact site annotation.
    :param anno: AnnotationObject containing one contact site.
    :return: arr Skeleton IDS, arr Skeleton axoness
    """
    #TODO: OUTDATED
    axoness = [[], []]
    cs_comment = anno.getComment()
    try:
        ids = re.findall('skel_(\d+)_(\d+)', cs_comment)[0]
    except IndexError:
        ids = re.findall('syn(\d+).k.zip_give_syn(\d+)', cs_comment)[0]
    for node in list(anno.getNodes()):
        n_comment = node.getComment()
        if 'skelnode' in n_comment:
            axoness_class = re.findall('axoness(\d+)', n_comment)[0]
            try:
                skel_id = re.findall('cs\d+_(\d+)_', n_comment)[0]
            except IndexError:
                skel_id = re.findall('syn(\d+).k.zip', n_comment)[0]
            axoness[ids.index(skel_id)] += [int(axoness_class)]
        #anno.addNode(node)
    axoness_0 = cell_classification(arr(axoness[0])) # int(np.round(np.mean(axoness[0])))
    axoness_1 = cell_classification(arr(axoness[1])) # int(np.round(np.mean(axoness[1])))
    axoness_comment = ids[0]+'axoness'+str(axoness_0) \
    + '_' + ids[1]+'axoness'+str(axoness_1)
    anno.appendComment(axoness_comment)
    return arr([int(ix) for ix in ids]), arr([axoness_0, axoness_1])


def predict_axoness_from_nodes(anno):
    """
    Exctracts axoness prediction from nodes for given contact site annotation.
    :param anno: AnnotationObject containing one contact site.
    :return: arr Skeleton IDS, arr Skeleton axoness
    """
    axoness = [[], []]
    cs_comment = anno.getComment()
    ids = []
    for node in list(anno.getNodes()):
        n_comment = node.getComment()
        if '_center' in n_comment:
            ids = [int(node.data['adj_skel1'])]
            ids.append(int(node.data['adj_skel2']))
            center_node = node
            break
    for node in list(anno.getNodes()):
        n_comment = node.getComment()
        if 'skelnode' in n_comment:
            axoness_class = node.data['axoness_pred']
            #skel_id = int(node.data['skel_id'])
            try:
                skel_id = int(re.findall('(\d+)_skelnode', n_comment)[0])
            except IndexError:
                skel_id = int(re.findall('syn(\d+)', n_comment)[0])
            axoness[ids.index(skel_id)] += [int(axoness_class)]
    axoness_0 = int(np.round(np.mean(axoness[0])))
    axoness_1 = int(np.round(np.mean(axoness[1])))
    axoness_comment = str(ids[0]) + 'axoness' + str(axoness_0) \
    + '_' + str(ids[1]) + 'axoness' + str(axoness_1)
    #TODO: save mean axoness at center node!
    anno.appendComment(axoness_comment)
    return arr(ids), arr([axoness_0, axoness_1])


def majority_processes(anno):
    """
    Label processes of cell in anno according to majority of axonoess in
    its nodes. If anno contains soma nodes, a slight smoothing is applied
    and afterwards the soma is grown out, in order to avoid branch points
    near the soma which are false positive axons/dendrite nodes.
    Inplace operation.
    :param anno: AnnotationObject
    """
    hns = []
    soma_node_nb = 0
    soma_node_ids = []
    for node in anno.getNodes():
        if node.degree() == 1:
            hns.append(node)
        if int(node.data["axoness_pred"]) == 2:
            soma_node_nb += 1
            soma_nodes_ids.append(node.getID())
    if soma_node_nb != 0:
        grow_out_soma(anno)
        majority_vote(anno, 'axoness', 3000)
        used_hn_ids = []
        graph = au.annotation_to_nx_graph(anno)
        calc_distance2soma(graph, hns)
        # reorder head nodes with descending distance to soma
        distances = [node.data['dist2soma'] for node in hns]
        hns = [hns[ii] for ii in np.argsort(distances)[::-1]]
        for hn in hns:
            if hn.getID() in used_hn_ids:
                continue
            else:
                visited_nodes = []
                axoness_found = []
                used_hn_ids.append(hn.getID())
            for node in nx.dfs_preorder_nodes(graph, hn):
                # if branch point stop
                if node.degree() == 1:
                    used_hn_ids.append(node.getID()) # probably redundant if iterate over
                                                # source node
                    # avoid false positive axon/dendrite branch point at soma
                # if int(node.data["dist2soma"]) <= 200:
                #     break
                if int(node.data["axoness_pred"]) == 2:
                    if len(axoness_found) == 0:
                        break
                    majority_axoness = cell_classification(arr(axoness_found))
                    for n in visited_nodes:
                        assign_property2node(n, majority_axoness, 'axoness')
                    break
                else:
                    visited_nodes.append(node)
                    axoness_found.append(int(node.data["axoness_pred"]))
        for n_ix in soma_node_ids:
            anno.getNodeByID(n_ix).data['axoness_pred'] = 2
    else:
        print "Process without soma prediction. Using majority vote of cell" \
              "part."
        axoness_found = []
        for node in anno.getNodes():
            axoness_found.append(int(node.data["axoness_pred"]))
        majority_axoness = cell_classification(arr(axoness_found))
        for node in anno.getNodes():
            assign_property2node(node, majority_axoness, 'axoness')


def calc_distance2soma(graph, nodes):
    """ Calculates the distance to a soma node for each node and sotres it
    in node.data['dist2soma']
    :param graph: graph of AnnotationObject
    :param nodes: Source nodes
    """
    for source in nodes:
        distance = 0
        current_coords = arr(source.getCoordinate_scaled())
        for node in nx.dfs_preorder_nodes(graph, source):
            new_coords = arr(node.getCoordinate_scaled())
            distance += np.linalg.norm(current_coords - new_coords)
            current_coords = new_coords
            if int(node.data['axoness_pred']) == 2:
                source.data['dist2soma'] = distance
                break


def grow_out_soma(anno, max_dist=700):
    """
    Grows out soma nodes, in order to overcome false negative soma nodes which
    should have separated axon and dendritic processes
    :param anno:
    :param max_dist:
    :return:
    """
    soma_nodes = []
    graph = au.annotation_to_nx_graph(anno)
    for node in anno.getNodes():
        if int(node.data["axoness_pred"]) == 2:
            soma_nodes.append(node)
    for source in soma_nodes:
        distance = 0
        current_coords = arr(source.getCoordinate_scaled())
        for node in nx.dfs_preorder_nodes(graph, source):
            new_coords = arr(node.getCoordinate_scaled())
            distance += np.linalg.norm(current_coords - new_coords)
            if distance > max_dist:
                break
            if int(node.data["axoness_pred"]) != 2:
                assign_property2node(node, 2, 'axoness')
            current_coords = new_coords


def get_soma_tracing_task_skels():
    paths = get_filepaths_from_dir('/lustre/pschuber/st250_pt3_minvotes18/'
                                   'nml_obj/')
    dest_path = '/lustre/pschuber/soma_tracing_skels/'
    if not os.path.isdir(dest_path):
        os.makedirs(dest_path)
    for ii, fpath in enumerate(paths):
        skel = load_ordered_mapped_skeleton(fpath)[0]
        dummy_skel = NewSkeleton()
        skel_id = get_skelID_from_path(fpath)
        skel.setComment(str(skel_id))
        dummy_skel.add_annotation(skel)
        dummy_anno = SkeletonAnnotation()
        dummy_anno.setComment('soma_' + str(skel_id))
        dummy_skel.add_annotation(dummy_anno)
        file_name = dest_path + 'task%i.k.zip' % ii
        dummy_skel.to_kzip(file_name)
        print "Wrote file %s" % (file_name)