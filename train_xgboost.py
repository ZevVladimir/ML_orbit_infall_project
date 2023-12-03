import numpy as np
import pandas as pd
import xgboost
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report  
import time 
import pickle
import os
from imblearn import under_sampling, over_sampling
from sklearn.model_selection import GridSearchCV
from sklearn.model_selection import StratifiedKFold
from data_and_loading_functions import build_ml_dataset, check_pickle_exist_gadget, choose_halo_split, create_directory
from visualization_functions import *
from xgboost_model_creator import model_creator, Iterator
##################################################################################################################
# LOAD CONFIG PARAMETERS
import configparser
config = configparser.ConfigParser()
config.read("config.ini")
curr_sparta_file = config["MISC"]["curr_sparta_file"]
path_to_MLOIS = config["PATHS"]["path_to_MLOIS"]
path_to_snaps = config["PATHS"]["path_to_snaps"]
path_to_SPARTA_data = config["PATHS"]["path_to_SPARTA_data"]
path_to_hdf5_file = path_to_SPARTA_data + curr_sparta_file + ".hdf5"
path_to_pickle = config["PATHS"]["path_to_pickle"]
path_to_calc_info = config["PATHS"]["path_to_calc_info"]
path_to_pygadgetreader = config["PATHS"]["path_to_pygadgetreader"]
path_to_sparta = config["PATHS"]["path_to_sparta"]
path_to_xgboost = config["PATHS"]["path_to_xgboost"]
create_directory(path_to_MLOIS)
create_directory(path_to_snaps)
create_directory(path_to_SPARTA_data)
create_directory(path_to_hdf5_file)
create_directory(path_to_pickle)
create_directory(path_to_calc_info)
create_directory(path_to_xgboost)
snap_format = config["MISC"]["snap_format"]
global prim_only
prim_only = config.getboolean("SEARCH","prim_only")
t_dyn_step = config.getfloat("SEARCH","t_dyn_step")
global p_snap
p_snap = config.getint("SEARCH","p_snap")
c_snap = config.getint("XGBOOST","c_snap")
model_name = config["XGBOOST"]["model_name"]
radii_splits = config.get("XGBOOST","rad_splits").split(',')
for split in radii_splits:
    model_name = model_name + "_" + str(split)

snapshot_list = [p_snap, c_snap]
global search_rad
search_rad = config.getfloat("SEARCH","search_rad")
total_num_snaps = config.getint("SEARCH","total_num_snaps")
per_n_halo_per_split = config.getfloat("SEARCH","per_n_halo_per_split")
test_halos_ratio = config.getfloat("SEARCH","test_halos_ratio")
curr_chunk_size = config.getint("SEARCH","chunk_size")
global num_save_ptl_params
num_save_ptl_params = config.getint("SEARCH","num_save_ptl_params")
##################################################################################################################
# set what the paths should be for saving and getting the data
if len(snapshot_list) > 1:
    specific_save = curr_sparta_file + "_" + str(snapshot_list[0]) + "to" + str(snapshot_list[-1]) + "_" + str(search_rad) + "r200msearch/"
else:
    specific_save = curr_sparta_file + "_" + str(snapshot_list[0]) + "_" + str(search_rad) + "r200msearch/"

save_location = path_to_xgboost + specific_save

with open(save_location + "train_keys.pickle", "rb") as pickle_file:
    train_all_keys = pickle.load(pickle_file)

create_directory(save_location)

# Determine where the scaled radii, rad vel, and tang vel are located within the dtaset
for i,key in enumerate(train_all_keys[2:]):
    if key == "Scaled_radii_" + str(p_snap):
        radii_loc = i
    elif key == "Radial_vel_" + str(p_snap):
        rad_vel_loc = i 
    elif key == "Tangential_vel_" + str(p_snap):
        tang_vel_loc = i

t0 = time.time()

# paths_to_train_data = []
# paths_to_val_data = []
# for path, subdirs, files in os.walk(path_to_xgboost + curr_sparta_file + "_" + str(p_snap) + "to" + str(c_snap) + "_" + str(search_rad) + "r200msearch/train_split_datasets/"):
#     for name in files:
#         paths_to_train_data.append(os.path.join(path, name))

# train_it = Iterator(paths_to_train_data)
# train_dataset = xgboost.DMatrix(train_it)

train_dataset = pickle.load(open("/home/zvladimi/MLOIS/xgboost_datasets_plots/sparta_cbol_l0063_n0256_190to178_6.0r200msearch/datasets/train_dataset.pickle", "rb"))
scale_pos_weight = np.where(train_dataset[:,1] == 0)[0].size / np.where(train_dataset[:,1] == 1)[0].size
print(scale_pos_weight)
train_dataset = xgboost.DMatrix(train_dataset[:,2:], label = train_dataset[:,1])

model_save_location = save_location + "models/" + model_name + "/"
create_directory(model_save_location)

curr_model_location = model_save_location + "range_all_" + curr_sparta_file + ".json"


if os.path.exists(curr_model_location) == False:
    model = None
    t3 = time.time()

    param = {
    'tree_method':'hist',
    'decice':'cuda',
    'eta': 0.01,
    'n_estimators': 100,
    'subsample': 0.1,
    'scale_pos_weight': scale_pos_weight,
    'seed': 11
    }
    # Train and fit each model with gpu
    model = xgboost.train(param, train_dataset)

    # le = LabelEncoder()
    # y = y.astype(np.int16)
    # y = le.fit_transform(y)

    t4 = time.time()
    print("Fitted model", t4 - t3, "seconds")

    model.save_model(curr_model_location)

else:
    model = xgboost.Booster()
    model.load_model(curr_model_location)
predicts = model.predict(train_dataset)
predicts = np.round(predicts)

# for i,path in enumerate(paths_to_train_data):
#     with open(path, "rb") as file:
#         curr_dataset = pickle.load(file)
#     if i == 0:
#         train_dataset = curr_dataset
#     else:
#         train_dataset = np.concatenate((train_dataset, curr_dataset), axis=0)
train_dataset = pickle.load(open("/home/zvladimi/MLOIS/xgboost_datasets_plots/sparta_cbol_l0063_n0256_190to178_6.0r200msearch/datasets/train_dataset.pickle", "rb"))
print(classification_report(train_dataset[:,1], predicts))
