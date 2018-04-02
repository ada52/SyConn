import glob
import numpy as np
from scipy import ndimage
import os
from ..handler.compression import MeshDict, VoxelDict, AttributeDict, SkeletonDict

script_folder = os.path.abspath(os.path.dirname(__file__) + "/../QSUB_scripts/")


def glia_pred_so(so, thresh, pred_key_appendix):
    assert so.type == "sv"
    pred_key = "glia_probas" + pred_key_appendix
    if not pred_key in so.attr_dict:
        so.load_attr_dict()
    preds = np.array(so.attr_dict[pred_key][:, 1] > thresh, dtype=np.int)
    pred = np.mean(so.attr_dict[pred_key][:, 1]) > thresh
    if pred == 0:
        return 0
    glia_votes = np.sum(preds)
    if glia_votes > int(len(preds) * 0.7):
        return 1
    return 0


def acquire_obj_ids(sd):
    """ Acquires all obj ids present in the dataset

    Loads id array if available. Assembles id list by iterating over all
    voxel / attribute dicts, otherwise (very slow).

    :param sd: SegmentationDataset

    """
    if os.path.exists(sd.path_ids):
        sd._ids = np.load(sd.path_ids)
    else:
        paths = glob.glob(sd.so_storage_path + "/*/*/*/") + \
                glob.glob(sd.so_storage_path + "/*/*/") + \
                glob.glob(sd.so_storage_path + "/*/")
        sd._ids = []
        for path in paths:
            if os.path.exists(path + "voxel.pkl"):
                this_ids = VoxelDict(path + "voxel.pkl",  read_only=True).keys()
            elif os.path.exists(path + "attr_dict.pkl"):
                this_ids = AttributeDict(path + "attr_dict.pkl", read_only=True).keys()
            else:
                this_ids = []

            sd._ids += this_ids

        sd._ids = np.array(sd._ids)
        np.save(sd.path_ids, sd._ids)


def save_voxels(so, bin_arr, offset, overwrite=False):
    assert bin_arr.dtype == bool

    voxel_dc = VoxelDict(so.voxel_path, read_only=False, timeout=3600,
                         disable_locking=True)

    if so.id in voxel_dc and not overwrite:
        voxel_dc.append(so.id, bin_arr, offset)
    else:
        voxel_dc[so.id] = [bin_arr], [offset]

    voxel_dc.save2pkl(so.voxel_path)


def load_voxels(so, voxel_dc=None):
    if voxel_dc is None:
        voxel_dc = VoxelDict(so.voxel_path, read_only=True,
                                   disable_locking=True)

    so._size = 0
    if so.id not in voxel_dc:
        print("Voxels for id %d do not exist" % so.id)
        return -1

    bin_arrs, block_offsets = voxel_dc[so.id]

    block_extents = []
    for i_bin_arr in range(len(bin_arrs)):
        block_extents.append(np.array(bin_arrs[i_bin_arr].shape) +
                             block_offsets[i_bin_arr])

    block_offsets = np.array(block_offsets)
    block_extents = np.array(block_extents)

    so._bounding_box = np.array([block_offsets.min(axis=0),
                                 block_extents.max(axis=0)])
    voxels = np.zeros(so.bounding_box[1] - so.bounding_box[0],
                      dtype=np.bool)

    for i_bin_arr in range(len(bin_arrs)):
        box = [block_offsets[i_bin_arr] - so.bounding_box[0],
               block_extents[i_bin_arr] - so.bounding_box[0]]

        so._size += np.sum(bin_arrs[i_bin_arr])

        voxels[box[0][0]: box[1][0],
               box[0][1]: box[1][1],
               box[0][2]: box[1][2]][bin_arrs[i_bin_arr]] = True

    return voxels


def load_voxels_downsampled(so, downsampling=(2, 2, 1)):
    if isinstance(so.voxels, int):
        return []

    return so.voxels[::downsampling[0], ::downsampling[1], ::downsampling[2]]


def load_voxel_list(so):
    voxel_list = np.array([], dtype=np.int32)

    if so._voxels is not None:
        voxel_list = np.array(zip(*np.nonzero(so.voxels)), dtype=np.int32) + \
                     so.bounding_box[0]
    else:
        voxel_dc = VoxelDict(so.voxel_path, read_only=True)
        bin_arrs, block_offsets = voxel_dc[so.id]

        for i_bin_arr in range(len(bin_arrs)):
            block_voxels = np.array(zip(*np.nonzero(bin_arrs[i_bin_arr])),
                                    dtype=np.int32)
            block_voxels += np.array(block_offsets[i_bin_arr])

            if len(voxel_list) == 0:
                voxel_list = block_voxels
            else:
                voxel_list = np.concatenate([voxel_list, block_voxels])

    return voxel_list


def load_voxel_list_downsampled(so, downsampling=(2, 2, 1)):
    downsampling = np.array(downsampling)
    dvoxels = so.load_voxels_downsampled(downsampling)
    voxel_list = np.array(zip(*np.nonzero(dvoxels)), dtype=np.int32)
    voxel_list = voxel_list * downsampling + np.array(so.bounding_box[0])

    return voxel_list


def load_voxel_list_downsampled_adapt(so, downsampling=(2, 2, 1)):
    downsampling = np.array(downsampling, dtype=np.int)
    dvoxels = so.load_voxels_downsampled(downsampling)

    if len(dvoxels) == 0:
        return []

    while True:
        if True in dvoxels:
            break

        downsampling /= 2
        downsampling[downsampling < 1] = 1
        dvoxels = so.load_voxels_downsampled(downsampling)

    voxel_list = np.array(zip(*np.nonzero(dvoxels)), dtype=np.int32)
    voxel_list = voxel_list * downsampling + np.array(so.bounding_box[0])

    return voxel_list


def load_mesh(so, recompute=False):
    if not recompute and so.mesh_exists:
        try:
            mesh = MeshDict(so.mesh_path,
                            disable_locking=not so.enable_locking)[so.id]
            if len(mesh) == 2:
                indices, vertices = mesh
                normals = np.zeros((0, ), dtype=np.float32)
            else:
                indices, vertices, normals = mesh
        except Exception as e:
            print("\n---------------------------------------------------\n"
                  "\n%s\nException occured when loading mesh.pkl of SO (%s)"
                  "with id %d."
                  "\n---------------------------------------------------\n"
                  % (e, so.type, so.id))
            return np.zeros((0, )).astype(np.int), np.zeros((0, )), np.zeros((0, ))
    else:
        if so.type == "sv":
            print("\n-----------------------\n"
                  "Mesh of SV %d not found.\n"
                  "-------------------------\n" % so.id)
            return np.zeros((0,)).astype(np.int), np.zeros((0,)), np.zeros((0, ))
        indices, vertices, normals = so._mesh_from_scratch()
        try:
            so._save_mesh(indices, vertices, normals)
        except Exception as e:
            print("\n-----------------------\n"
                  "Mesh of %s %d could not be saved:\n%s\n"
                  "-------------------------\n" % (so.type, so.id, e))
    vertices = np.array(vertices, dtype=np.int)
    indices = np.array(indices, dtype=np.int)
    normals = np.array(normals, dtype=np.float32)
    return indices, vertices, normals


def load_skeleton(so, recompute=False):
    if not recompute and so.skeleton_exists:
        try:
            skeleton_dc = SkeletonDict(so.skeleton_path,
                                       disable_locking=not so.enable_locking)
            nodes, diameters, edges = skeleton_dc[so.id]['nodes'], skeleton_dc[so.id]['diameters'], skeleton_dc[so.id]['edges']
        except Exception as e:
            print("\n---------------------------------------------------\n"
                  "\n%s\nException occured when loading skeletons.pkl of SO (%s)"
                  "with id %d."
                  "\n---------------------------------------------------\n"
                  % (e, so.type, so.id))
            return np.zeros((0, )).astype(np.int), np.zeros((0, )),np.zeros((0,)).astype(np.int)
    else:
        if so.type == "sv":
            print("\n-----------------------\n"
                  "Skeleton of SV %d (size: %d) not found.\n"
                  "-------------------------\n" % (so.id, so.size))
            return np.zeros((0,)).astype(np.int), np.zeros((0,)),np.zeros((0,)).astype(np.int)

    nodes = np.array(nodes, dtype=np.int)
    diameters = np.array(diameters, dtype=np.float)
    edges = np.array(edges, dtype=np.int)

    return nodes, diameters, edges


def binary_closing(vx, n_iterations=13):
    n_iterations = int(n_iterations)
    assert n_iterations > 0

    vx = np.pad(vx, [[n_iterations]*2]*3, mode='constant')

    vx = ndimage.morphology.binary_closing(vx, iterations=n_iterations)

    vx = vx[n_iterations: -n_iterations,
            n_iterations: -n_iterations,
            n_iterations: -n_iterations]

    return vx
