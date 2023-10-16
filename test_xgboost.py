import numpy as np
import pandas as pd
import matplotlib as mpl
import matplotlib.pyplot as plt
import h5py
from xgboost import XGBClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report  
import time 
import pickle
import os
from imblearn import under_sampling, over_sampling
from pairing import depair
from colossus.cosmology import cosmology
from colossus.lss import peaks
##################################################################################################################
# General params
snapshot_list = [190,184] # SHOULD BE DESCENDING
p_snap = snapshot_list[0]
times_r200m = 6
curr_sparta_file = "sparta_cbol_l0063_n0256"
path_to_hdf5_file = "/home/zvladimi/MLOIS/SPARTA_data/" + curr_sparta_file + ".hdf5"
path_dict = {
    "curr_sparta_file": curr_sparta_file,
    "path_to_MLOIS": "/home/zvladimi/MLOIS/",
    "path_to_snaps": "/home/zvladimi/MLOIS/particle_data/",
    "path_to_hdf5_file": path_to_hdf5_file,
    "path_to_pickle": "/home/zvladimi/MLOIS/pickle_data/",
    "path_to_datasets": "/home/zvladimi/MLOIS/calculated_info/",
    "path_to_model_plots": "/home/zvladimi/MLOIS/xgboost_datasets_plots/"
}
snap_format = "{:04d}" # how are the snapshots formatted with 0s
##################################################################################################################
# import pygadgetreader and sparta
import sys
sys.path.insert(0, path_dict["path_to_MLOIS"] + "pygadgetreader")
sys.path.insert(0, path_dict["path_to_MLOIS"] + "sparta/analysis")
from pygadgetreader import readsnap, readheader
from sparta import sparta
from data_and_loading_functions import build_ml_dataset, check_pickle_exist_gadget, choose_halo_split, create_directory, load_or_pickle_SPARTA_data
from visualization_functions import *
#from train_xgboost_models import model_creator
##################################################################################################################
# set what the paths should be for saving and getting the data
if len(snapshot_list) > 1:
    specific_save = curr_sparta_file + "_" + str(snapshot_list[0]) + "to" + str(snapshot_list[-1]) + "_" + str(times_r200m) + "r200msearch/"
else:
    specific_save = curr_sparta_file + "_" + str(snapshot_list[0]) + "_" + str(times_r200m) + "r200msearch/"

path_to_datasets = path_dict["path_to_model_plots"] + specific_save + "datasets/"
data_location = path_dict["path_to_datasets"] + specific_save
save_location = path_dict["path_to_model_plots"] + specific_save

create_directory(save_location)

p_snapshot_path = path_dict["path_to_snaps"] + "snapdir_" + snap_format.format(p_snap) + "/snapshot_" + snap_format.format(p_snap)

p_red_shift = readheader(p_snapshot_path, 'redshift')
p_scale_factor = 1/(1+p_red_shift)
halos_pos, halos_r200m, halos_id, halos_status, halos_last_snap, ptl_mass = load_or_pickle_SPARTA_data(curr_sparta_file, p_scale_factor, p_snap, path_dict)
cosmol = cosmology.setCosmology("bolshoi")

with open(path_to_datasets + "test_dataset_all_keys.pickle", "rb") as pickle_file:
    test_all_keys = pickle.load(pickle_file)
with open(path_to_datasets + "test_dataset_" + curr_sparta_file + "_" + str(snapshot_list[0]) + "_" + str(snapshot_list[-1]) + ".pickle", "rb") as pickle_file:
    test_dataset = pickle.load(pickle_file)

num_params_per_snap = (len(test_all_keys) - 2) / len(snapshot_list)

num_bins = 30

all_file_names = []
all_models = []
for filename in os.listdir(save_location + "/models/"):
    all_file_names.append(filename)
all_file_names.sort()

for filename in all_file_names:
    with open(save_location + "/models/" + filename, "rb") as pickle_file:
        model = pickle.load(pickle_file)
    model.load_models()
    all_models.append(model)
    
# Determine where the scaled radii, rad vel, and tang vel are located within the dtaset
for i,key in enumerate(test_all_keys[2:]):
    if key == "Scaled_radii_" + str(p_snap):
        scaled_radii_loc = i
    elif key == "Radial_vel_" + str(p_snap):
        rad_vel_loc = i
    elif key == "Tangential_vel_" + str(p_snap):
        tang_vel_loc = i

    
with open(data_location + "test_indices.pickle", "rb") as pickle_file:
    test_indices = pickle.load(pickle_file)
use_halo_ids = halos_id[test_indices]
sparta_output = sparta.load(filename=path_dict["path_to_hdf5_file"], halo_ids=use_halo_ids)
dens_prf_all = sparta_output['anl_prf']['M_all'][:,p_snap,:]
dens_prf_1halo = sparta_output['anl_prf']['M_1halo'][:,p_snap,:]

# test indices are the indices of the match halo idxs used (see find_particle_properties_ML.py to see how test_indices are created)
num_test_halos = test_indices.shape[0]

# for every halo idx and pid paired in the test dataset get the halo idxs
test_halo_idxs = np.zeros(test_dataset.shape[0])
use_ptl_ids = np.zeros(test_dataset.shape[0])

for i,id in enumerate(test_dataset[:,0]):
    depaired = depair(id)
    use_ptl_ids[i] = depaired[0]
    test_halo_idxs[i] = depaired[1]

halo_masses = np.zeros(num_test_halos)

start = 0
all_accuracy = []
# get the mases for each halo
for j in range(num_test_halos):
    curr_halo_idx = test_indices[j]
    curr_test_halo = test_dataset[np.where(test_halo_idxs == curr_halo_idx)]
    halo_masses[j] = curr_test_halo.shape[0] * ptl_mass

p_red_shift = readheader(p_snapshot_path, 'redshift')
peak_heights = peaks.peakHeight(halo_masses, p_red_shift)
                
start_nu = np.min(peak_heights) 
nu_step = np.max(peak_heights)
num_iter = 1

for i in range(num_iter):
    end_nu = start_nu + nu_step

    idx_within_nu = np.where((peak_heights >= start_nu) & (peak_heights < end_nu))[0]
    curr_test_halo_idxs = test_indices[idx_within_nu]
    print(start_nu, "to", end_nu, ":", idx_within_nu.shape, "halos")
    
    if curr_test_halo_idxs.shape[0] != 0:
        density_prf_all_within = np.zeros(dens_prf_all.shape[1])
        density_prf_1halo_within = np.zeros(dens_prf_1halo.shape[1])
        for j, idx in enumerate(curr_test_halo_idxs):
            density_prf_all_within = density_prf_all_within + dens_prf_all[j]
            density_prf_1halo_within = density_prf_1halo_within + dens_prf_1halo[j]
            use_ptl_idxs = np.where(test_halo_idxs == idx)
            if j == 0:
                test_halos_within = test_dataset[use_ptl_idxs]
                continue
            else:
                curr_test_halo = test_dataset[use_ptl_idxs]
                test_halos_within = np.row_stack((test_halos_within, curr_test_halo))

        test_predict = np.ones(test_halos_within.shape[0]) * -1

        for i in range(len(snapshot_list)):
            curr_dataset = test_halos_within[:,:int(2 + (num_params_per_snap * (i+1)))]

            if i == (len(snapshot_list)-1):
                use_ptls = np.where(test_halos_within[:,-1]!= 0)[0]
            else:
                use_ptls = np.where(test_halos_within[:,int(2 + (num_params_per_snap * (i+1)))] == 0)[0]

            curr_dataset = curr_dataset[use_ptls]
            all_models[i].predict(curr_dataset)
            test_predict[use_ptls] = all_models[i].get_predicts()

        actual_labels = test_halos_within[:,1]

        classification = classification_report(actual_labels, test_predict, output_dict=True)

        all_accuracy.append(classification["accuracy"])
        bins = sparta_output["config"]['anl_prf']["r_bins_lin"]
        bins = np.insert(bins, 0, 0)
        compare_density_prf(radii=test_halos_within[:,2+scaled_radii_loc], actual_prf_all=density_prf_all_within, actual_prf_1halo=density_prf_1halo_within, mass=ptl_mass, orbit_assn=test_predict, prf_bins=bins, title = str(np.floor(start_nu)) + "-" + str(np.ceil(end_nu)), show_graph = False, save_graph = True, save_location = save_location)
        plot_radius_rad_vel_tang_vel_graphs(test_predict, test_halos_within[:,2+scaled_radii_loc], test_halos_within[:,2+rad_vel_loc], test_halos_within[:,2+tang_vel_loc], actual_labels, "ML Predictions", num_bins, np.floor(start_nu), np.ceil(end_nu), show = False, save = True, save_location=save_location)
        graph_acc_by_bin(test_predict, actual_labels, test_halos_within[:,2+scaled_radii_loc], num_bins, np.floor(start_nu), np.ceil(end_nu), plot = False, save = True, save_location = save_location)
        
    start_nu = end_nu
    
print(all_accuracy)
fig, ax = plt.subplots(1,1)
ax.set_title("Number of halos at each accuracy level")
ax.set_xlabel("Accuracy")
ax.set_ylabel("Num halos")
ax.hist(all_accuracy)
fig.savefig(save_location + "all_test_halo_acc.png")