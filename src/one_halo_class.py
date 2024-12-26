from dask import array as da
from dask.distributed import Client
from dask_cuda import LocalCUDACluster

from contextlib import contextmanager

import xgboost as xgb
from xgboost import dask as dxgb
from xgboost.dask import DaskDMatrix
from sklearn.metrics import classification_report
import pickle
import os
import sys
import numpy as np
import json
import re
from colossus.cosmology import cosmology
import multiprocessing as mp

from utils.ML_support import *
from utils.data_and_loading_functions import *
from utils.update_vis_fxns import plot_halo_slice_class, plot_halo_3d_class
##################################################################################################################
# LOAD CONFIG PARAMETERS
import configparser
config = configparser.ConfigParser()
config.read(os.getcwd() + "/config.ini")
on_zaratan = config.getboolean("MISC","on_zaratan")
use_gpu = config.getboolean("MISC","use_gpu")
curr_sparta_file = config["MISC"]["curr_sparta_file"]
path_to_MLOIS = config["PATHS"]["path_to_MLOIS"]
path_to_snaps = config["PATHS"]["path_to_snaps"]
path_to_SPARTA_data = config["PATHS"]["path_to_SPARTA_data"]
sim_cosmol = config["MISC"]["sim_cosmol"]
if sim_cosmol == "planck13-nbody":
    sim_pat = r"cpla_l(\d+)_n(\d+)"
else: 
    sim_pat = r"cbol_l(\d+)_n(\d+)"
match = re.search(sim_pat, curr_sparta_file)
if match:
    sparta_name = match.group(0)
path_to_hdf5_file = path_to_SPARTA_data + sparta_name + "/" + curr_sparta_file + ".hdf5"
path_to_pickle = config["PATHS"]["path_to_pickle"]
path_to_calc_info = config["PATHS"]["path_to_calc_info"]
path_to_pygadgetreader = config["PATHS"]["path_to_pygadgetreader"]
path_to_sparta = config["PATHS"]["path_to_sparta"]
path_to_xgboost = config["PATHS"]["path_to_xgboost"]

sim_cosmol = config["MISC"]["sim_cosmol"]
t_dyn_step = config.getfloat("SEARCH","t_dyn_step")
p_red_shift = config.getfloat("SEARCH","p_red_shift")
radii_splits = config.get("XGBOOST","rad_splits").split(',')
search_rad = config.getfloat("SEARCH","search_rad")
total_num_snaps = config.getint("SEARCH","total_num_snaps")
test_halos_ratio = config.getfloat("XGBOOST","test_halos_ratio")
curr_chunk_size = config.getint("SEARCH","chunk_size")
num_save_ptl_params = config.getint("SEARCH","num_save_ptl_params")
do_hpo = config.getboolean("XGBOOST","hpo")
# size float32 is 4 bytes
chunk_size = int(np.floor(1e9 / (num_save_ptl_params * 4)))
frac_training_data = 1
model_sims = json.loads(config.get("XGBOOST","model_sims"))
model_type = config["XGBOOST"]["model_type"]
test_sims = json.loads(config.get("XGBOOST","test_sims"))
eval_datasets = json.loads(config.get("XGBOOST","eval_datasets"))

dens_prf_plt = config.getboolean("XGBOOST","dens_prf_plt")
misclass_plt = config.getboolean("XGBOOST","misclass_plt")
fulldist_plt = config.getboolean("XGBOOST","fulldist_plt")
io_frac_plt = config.getboolean("XGBOOST","io_frac_plt")
per_err_plt = config.getboolean("XGBOOST","per_err_plt")

if sim_cosmol == "planck13-nbody":
    cosmol = cosmology.setCosmology('planck13-nbody',{'flat': True, 'H0': 67.0, 'Om0': 0.32, 'Ob0': 0.0491, 'sigma8': 0.834, 'ns': 0.9624, 'relspecies': False})
else:
    cosmol = cosmology.setCosmology(sim_cosmol) 

if use_gpu:
    from cuml.metrics.accuracy import accuracy_score #TODO fix cupy installation??
    from sklearn.metrics import make_scorer
    import dask_ml.model_selection as dcv
    import cudf
elif not use_gpu and on_zaratan:
    from dask_mpi import initialize
    from mpi4py import MPI
    from distributed.scheduler import logger
    import socket
    #from dask_jobqueue import SLURMCluster

###############################################################################################################

if __name__ == "__main__":
    feature_columns = ["p_Scaled_radii","p_Radial_vel","p_Tangential_vel","c_Scaled_radii","c_Radial_vel","c_Tangential_vel"]
    target_column = ["Orbit_infall"]
    
    if use_gpu:
        mp.set_start_method("spawn")

    if not use_gpu and on_zaratan:
        if 'SLURM_CPUS_PER_TASK' in os.environ:
            cpus_per_task = int(os.environ['SLURM_CPUS_PER_TASK'])
        else:
            print("SLURM_CPUS_PER_TASK is not defined.")
        if use_gpu:
            initialize(local_directory = "/home/zvladimi/scratch/MLOIS/dask_logs/")
        else:
            initialize(nthreads = cpus_per_task, local_directory = "/home/zvladimi/scratch/MLOIS/dask_logs/")
        print("Initialized")
        client = Client()
        host = client.run_on_scheduler(socket.gethostname)
        port = client.scheduler_info()['services']['dashboard']
        login_node_address = "zvladimi@login.zaratan.umd.edu" # Change this to the address/domain of your login node

        logger.info(f"ssh -N -L {port}:{host}:{port} {login_node_address}")
    else:
        client = get_CUDA_cluster()
    
    model_comb_name = get_combined_name(model_sims) 
    scale_rad=False
    use_weights=False
    if reduce_rad > 0 and reduce_perc > 0:
        scale_rad = True
    if weight_rad > 0 and min_weight > 0:
        use_weights=True    
    
    model_dir = model_type + "_" + model_comb_name + "nu" + nu_string 
    
    if scale_rad:
        model_dir += "scl_rad" + str(reduce_rad) + "_" + str(reduce_perc)
    if use_weights:
        model_dir += "wght" + str(weight_rad) + "_" + str(min_weight)
        
    # model_name =  model_dir + model_comb_name
    
    model_save_loc = path_to_xgboost + model_comb_name + "/" + model_dir + "/"

    try:
        bst = xgb.Booster()
        bst.load_model(model_save_loc + model_dir + ".json")
        bst.set_param({"device": "cuda:0"})
        print("Loaded Model Trained on:",model_sims)
    except:
        print("Couldn't load Booster Located at: " + model_save_loc + model_dir + ".json")
        
    try:
        with open(model_save_loc + "model_info.pickle", "rb") as pickle_file:
            model_info = pickle.load(pickle_file)
    except FileNotFoundError:
        print("Model info could not be loaded please ensure the path is correct or rerun train_xgboost.py")
    
    #TODO adjust this?
    # Only takes FIRST SIM

    sim = test_sims[0][0]

    model_comb_name = get_combined_name(model_sims) 

    model_dir = model_type + "_" + model_comb_name + "nu" + nu_string 

    model_save_loc = path_to_xgboost + model_comb_name + "/" + model_dir + "/"
    dset_name = "Test"
    test_comb_name = get_combined_name(test_sims[0]) 

    plot_loc = model_save_loc + dset_name + "_" + test_comb_name + "/plots/"
    create_directory(plot_loc)

    halo_ddf = reform_df(path_to_calc_info + sim + "/" + "Test" + "/halo_info/")
    all_idxs = halo_ddf["Halo_indices"].values

    with open(path_to_calc_info + sim + "/p_ptl_tree.pickle", "rb") as pickle_file:
        tree = pickle.load(pickle_file)
            
    sparta_name, sparta_search_name = split_calc_name(sim)
    # find the snapshots for this simulation
    snap_pat = r"(\d+)to(\d+)"
    match = re.search(snap_pat, sim)
    if match:
        curr_snap_list = [match.group(1), match.group(2)]   
        p_snap = int(curr_snap_list[0])

    with open(path_to_calc_info + sim + "/config.pickle", "rb") as file:
        config_dict = pickle.load(file)
        
        curr_z = config_dict["p_snap_info"]["red_shift"][()]
        curr_snap_dir_format = config_dict["snap_dir_format"]
        curr_snap_format = config_dict["snap_format"]
        new_p_snap, curr_z = find_closest_z(curr_z,path_to_snaps + sparta_name + "/",curr_snap_dir_format,curr_snap_format)
        p_scale_factor = 1/(1+curr_z)
        
    with h5py.File(path_to_SPARTA_data + sparta_name + "/" + sparta_search_name + ".hdf5","r") as f:
        dic_sim = {}
        grp_sim = f['simulation']

        for attr in grp_sim.attrs:
            dic_sim[attr] = grp_sim.attrs[attr]

    all_red_shifts = dic_sim['snap_z']
    p_sparta_snap = np.abs(all_red_shifts - curr_z).argmin()

    halos_pos, halos_r200m, halos_id, halos_status, halos_last_snap, parent_id, ptl_mass = load_or_pickle_SPARTA_data(sparta_search_name, p_scale_factor, p_snap, p_sparta_snap)

    snap_loc = path_to_snaps + sparta_name + "/"
    p_snapshot_path = snap_loc + "snapdir_" + snap_dir_format.format(p_snap) + "/snapshot_" + snap_format.format(p_snap)
    ptls_pid, ptls_vel, ptls_pos = load_or_pickle_ptl_data(curr_sparta_file, str(p_snap), p_snapshot_path, p_scale_factor)
            

    halo_files = []
    halo_dfs = []
    if dset_name == "Full":    
        halo_dfs.append(reform_df(path_to_calc_info + sim + "/" + "Train" + "/halo_info/"))
        halo_dfs.append(reform_df(path_to_calc_info + sim + "/" + "Test" + "/halo_info/"))
    else:
        halo_dfs.append(reform_df(path_to_calc_info + sim + "/" + dset_name + "/halo_info/"))

    halo_df = pd.concat(halo_dfs)
    
    data,scale_pos_weight = load_data(client,test_sims[0],dset_name,limit_files=False)

    X = data[feature_columns]
    y = data[target_column]
    
    halo_n = halo_df["Halo_n"].values
    halo_first = halo_df["Halo_first"].values

    sorted_indices = np.argsort(halo_n)[::-1]
    large_loc = sorted_indices[-25]
    
    all_idxs = halo_ddf["Halo_indices"].values
    use_idx = all_idxs[large_loc]
    
    use_halo_pos = halos_pos[use_idx]
    use_halo_r200m = halos_r200m[use_idx]
    use_halo_id = halos_id[use_idx]

    ptl_indices = tree.query_ball_point(use_halo_pos, r = search_rad * use_halo_r200m)
    ptl_indices = np.array(ptl_indices)
    # print(ptls_pos)
    # print(use_halo_pos)
    # print(use_halo_r200m)
    curr_ptl_pos = ptls_pos[ptl_indices]
    curr_ptl_pids = ptls_pid[ptl_indices]

    num_new_ptls = curr_ptl_pos.shape[0]

    sparta_output = sparta.load(filename = path_to_hdf5_file, halo_ids=use_halo_id, log_level=0)

    sparta_last_pericenter_snap = sparta_output['tcr_ptl']['res_oct']['last_pericenter_snap']
    sparta_n_pericenter = sparta_output['tcr_ptl']['res_oct']['n_pericenter']
    sparta_tracer_ids = sparta_output['tcr_ptl']['res_oct']['tracer_id']
    sparta_n_is_lower_limit = sparta_output['tcr_ptl']['res_oct']['n_is_lower_limit']

    compare_sparta_assn = np.zeros((sparta_tracer_ids.shape[0]))
    curr_orb_assn = np.zeros((num_new_ptls))
        # Anywhere sparta_last_pericenter is greater than the current snap then that is in the future so set to 0
    future_peri = np.where(sparta_last_pericenter_snap > p_snap)[0]
    adj_sparta_n_pericenter = sparta_n_pericenter
    adj_sparta_n_pericenter[future_peri] = 0
    adj_sparta_n_is_lower_limit = sparta_n_is_lower_limit
    adj_sparta_n_is_lower_limit[future_peri] = 0
    # If a particle has a pericenter or if the lower limit is 1 then it is orbiting

    compare_sparta_assn[np.where((adj_sparta_n_pericenter >= 1) | (adj_sparta_n_is_lower_limit == 1))[0]] = 1
    # compare_sparta_assn[np.where(adj_sparta_n_pericenter >= 1)] = 1

    # Compare the ids between SPARTA and the found prtl ids and match the SPARTA results
    matched_ids = np.intersect1d(curr_ptl_pids, sparta_tracer_ids, return_indices = True)
    curr_orb_assn[matched_ids[1]] = compare_sparta_assn[matched_ids[2]]
    preds = make_preds(client, bst, X, report_name="Report", print_report=False)
    preds = preds.iloc[halo_first[large_loc]:halo_first[large_loc] + halo_n[large_loc]]

    plot_halo_slice_class(curr_ptl_pos,preds,curr_orb_assn,use_halo_pos,use_halo_r200m,plot_loc)
    plot_halo_3d_class(curr_ptl_pos,preds,curr_orb_assn,use_halo_pos,use_halo_r200m,plot_loc)