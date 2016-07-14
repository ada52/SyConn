import sys
from syconn.processing.mapper import  prepare_syns_btw_annos
import cPickle as pickle
__author__ = 'pschuber'


if __name__ == '__main__':

    path_storage_file = sys.argv[1]
    path_out_file = sys.argv[2]

    with open(path_storage_file) as f:
        pairwise_paths = pickle.load(f)
        dest_path = pickle.load(f)

    prepare_syns_btw_annos(pairwise_paths, dest_path)