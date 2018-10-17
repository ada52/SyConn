[![Documentation Status](https://readthedocs.org/projects/syconn/badge/?version=latest)](https://syconn.readthedocs.io/en/latest/?badge=latest)

# SyConn
Refactored (still an early stage construction) version of SyConn for automated synaptic connectivity inference based on dense EM segmentation data.
For v0.1 see the SyConn branch [dorkenwald2017nm](https://github.com/StructuralNeurobiologyLab/SyConn/tree/dorkenwald2017nm).

v0.2 currently features:
- introduction of supervoxel and agglomerated supervoxel classes
- added support for (sub-) cellular compartment (spines, axon/dendrite/soma) and cell type classification with [skeleton](https://www.nature.com/articles/nmeth.4206)- and [multiview-based](https://www.biorxiv.org/content/early/2018/07/06/364034) approaches
- cell organelle prediction, extraction and mesh generation
- [glia identification and splitting](https://www.biorxiv.org/content/early/2018/07/06/364034)
- generation of connectivity matrix

## System requirements & installation
* Python 3.5
* The whole pipeline was designed and tested on Linux systems (CentOS, Arch)
* SyConn is primarily based on the packages [elektronn](http://elektronn.org) and [knossos-utils](https://github.com/knossos-project/knossos_utils)
* We use [KNOSSOS](https://knossostool.org/)
 for visualization and annotation of 3D EM data sets.
* [VIGRA](https://ukoethe.github.io/vigra/), e.g. ``conda install -c ukoethe vigra``
* osmesa, e.g.: ``conda install -c menpo osmesa``

You can install SyConn using  ``git`` and  ``pip``:

    git clone https://github.com/SyConn
    cd SyConn
    pip install -r requirements.txt
    pip install .

## Tutorials & documentation

For the SyConn documentation see [here](docs/doc.md) or check out the latest readthedocs build [here](https://syconn.readthedocs.io/en/latest/). Alternatively you can build the API documentation locally by running `make html` in the `docs` folder.

# The Team
The Synaptic connectivity inference toolkit is developed at Max-Planck-Institute of Neurobiology, Munich.

Authors: Philipp Schubert, Sven Dorkenwald, Rangoli Saxena, Joergen Kornfeld
