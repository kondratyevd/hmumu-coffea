import os
from functools import partial

import dask.dataframe as dd
import pandas as pd
import numpy as np
import pickle
from hist import Hist
from nanoaod.config.variables import variables_lookup, Variable
from python.io import load_pandas_from_parquet
from python.io import save_histogram

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
pd.options.mode.chained_assignment = None


training_features = [
    "dimuon_mass",
    "dimuon_pt",
    "dimuon_pt_log",
    "dimuon_eta",
    "dimuon_mass_res",
    "dimuon_mass_res_rel",
    "dimuon_cos_theta_cs",
    "dimuon_phi_cs",
    "jet1_pt",
    "jet1_eta",
    "jet1_phi",
    "jet1_qgl",
    "jet2_pt",
    "jet2_eta",
    "jet2_phi",
    "jet2_qgl",
    "jj_mass",
    "jj_mass_log",
    "jj_dEta",
    "rpt",
    "ll_zstar_log",
    "mmj_min_dEta",
    "nsoftjets5",
    "htsoft2",
]

training_features = [
    "dimuon_mass",
    "dimuon_pt",
    "dimuon_pt_log",
    "dimuon_eta",
    "dimuon_ebe_mass_res",
    "dimuon_ebe_mass_res_rel",
    "dimuon_cos_theta_cs",
    "dimuon_phi_cs",
    "jet1_pt",
    "jet1_eta",
    "jet1_phi",
    "jet1_qgl",
    "jet2_pt",
    "jet2_eta",
    "jet2_phi",
    "jet2_qgl",
    "jj_mass",
    "jj_mass_log",
    "jj_dEta",
    "rpt",
    "ll_zstar_log",
    "mmj_min_dEta",
    "nsoftjets5",
    "htsoft2",
]

decorrelation_scheme = {
    "LHERen": {"DY": ["DY"], "EWK": ["EWK"], "ggH": ["ggH"], "TT+ST": ["TT+ST"]},
    "LHEFac": {"DY": ["DY"], "EWK": ["EWK"], "ggH": ["ggH"], "TT+ST": ["TT+ST"]},
    "pdf_2rms": {"DY": ["DY"], "ggH": ["ggH"], "VBF": ["VBF"]},
    "pdf_mcreplica": {"DY": ["DY"], "ggH": ["ggH"], "VBF": ["VBF"]},
    # 'LHERen': {'DY':['DY_filter', 'DY_nofilter'], 'EWK':['EWK'],
    #            'ggH':['ggH'], 'TT+ST':['TT+ST']},
    # 'LHEFac': {'DY':['DY_filter', 'DY_nofilter'], 'EWK':['EWK'],
    #            'ggH':['ggH'], 'TT+ST':['TT+ST']},
    # 'pdf_2rms': {'DY':['DY_filter', 'DY_nofilter'],
    #             'ggH':['ggH'], 'VBF':['VBF']},
    # 'pdf_mcreplica': {'DY':['DY_filter', 'DY_nofilter'],
    #                  'ggH':['ggH'], 'VBF':['VBF']},
}


def workflow(client, paths, parameters, timer=None):
    # Load dataframes
    df_future = client.map(load_pandas_from_parquet, paths)
    df_future = client.gather(df_future)
    if timer:
        timer.add_checkpoint("Loaded data from Parquet")

    # Merge dataframes
    try:
        df = dd.concat([d for d in df_future if len(d.columns) > 0])
    except Exception:
        return
    npart = df.npartitions
    df = df.compute()
    df.reset_index(inplace=True, drop=True)
    df = dd.from_pandas(df, npartitions=npart)
    if npart > 2 * parameters["ncpus"]:
        df = df.repartition(npartitions=parameters["ncpus"])
    if timer:
        timer.add_checkpoint("Combined into a single Dask DataFrame")

    # temporary
    if ("dataset" not in df.columns) and ("s" in df.columns):
        df["dataset"] = df["s"]
    if ("region" not in df.columns) and ("r" in df.columns):
        df["region"] = df["r"]
    for v in parameters["syst_variations"]:
        if (f"channel {v}" not in df.columns) and (f"c {v}" in df.columns):
            df[f"channel {v}"] = df[f"c {v}"]

    keep_columns = ["dataset", "year", "region"]
    keep_columns += [f"channel {v}" for v in parameters["syst_variations"]]
    keep_columns += [c for c in df.columns if "wgt_" in c]
    keep_columns += parameters["hist_vars"]

    # Evaluate classifiers
    # TODO: outsource to GPUs
    evaluate_mva = True
    if evaluate_mva:
        for v in parameters["syst_variations"]:
            for model in parameters["dnn_models"]:
                score_name = f"score_{model} {v}"
                keep_columns += [score_name]
                df[score_name] = df.map_partitions(
                    dnn_evaluation, v, model, parameters, meta=(score_name, float)
                )
                if timer:
                    timer.add_checkpoint(f"Evaluated {model} {v}")
            for model in parameters["bdt_models"]:
                score_name = f"score_{model} {v}"
                keep_columns += [score_name]
                df[score_name] = df.map_partitions(
                    bdt_evaluation, v, model, parameters, meta=(score_name, float)
                )
                if timer:
                    timer.add_checkpoint(f"Evaluated {model} {v}")
    df = df[[c for c in keep_columns if c in df.columns]]
    df = df.compute()
    df.dropna(axis=1, inplace=True)
    df.reset_index(inplace=True)
    if timer:
        timer.add_checkpoint("Prepared for histogramming")

    argsets = []
    for year in df.year.unique():
        for var_name in parameters["hist_vars"]:
            for dataset in df.dataset.unique():
                argsets.append({"year": year, "var_name": var_name, "dataset": dataset})
    # Make histograms
    hist_futures = client.map(partial(histogram, df=df, parameters=parameters), argsets)
    hist_rows = client.gather(hist_futures)
    hist_df = pd.concat(hist_rows).reset_index(drop=True)
    if timer:
        timer.add_checkpoint("Histogramming")
    return hist_df


def prepare_features(df, parameters, variation="nominal", add_year=True):
    global training_features
    if add_year:
        features = training_features + ["year"]
    else:
        features = training_features
    features_var = []
    for trf in features:
        if f"{trf} {variation}" in df.columns:
            features_var.append(f"{trf} {variation}")
        elif trf in df.columns:
            features_var.append(trf)
        else:
            print(f"Variable {trf} not found in training dataframe!")
    return features_var


def dnn_evaluation(df, variation, model, parameters):
    import tensorflow as tf
    from tensorflow.keras.models import load_model

    config = tf.compat.v1.ConfigProto(
        intra_op_parallelism_threads=1,
        inter_op_parallelism_threads=1,
        allow_soft_placement=True,
        device_count={"CPU": 1},
    )
    tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.FATAL)
    sess = tf.compat.v1.Session(config=config)
    if parameters["do_massscan"]:
        mass_shift = parameters["mass"] - 125.0
    features = prepare_features(df, parameters, variation, add_year=True)
    score_name = f"score_{model} {variation}"
    try:
        df = df.compute()
    except Exception:
        pass
    df.loc[:, score_name] = 0
    if df.shape[0] == 0:
        return df[score_name]

    with sess:
        nfolds = 4
        for i in range(nfolds):
            # FIXME
            label = f"allyears_jul7_{i}"

            # train_folds = [(i + f) % nfolds for f in [0, 1]]
            # val_folds = [(i + f) % nfolds for f in [2]]
            eval_folds = [(i + f) % nfolds for f in [3]]

            eval_filter = df.event.mod(nfolds).isin(eval_folds)

            scalers_path = f"{parameters['models_path']}/{model}/scalers_{label}.npy"
            scalers = np.load(scalers_path)
            model_path = f"{parameters['models_path']}/{model}/dnn_{label}.h5"
            dnn_model = load_model(model_path)
            df_i = df.loc[eval_filter, :]
            if df_i.shape[0] == 0:
                continue
            df_i.loc[df_i.region != "h-peak", "dimuon_mass"] = 125.0
            if parameters["do_massscan"]:
                df_i.loc[:, "dimuon_mass"] = df_i["dimuon_mass"] - mass_shift
            df_i = (df_i[features] - scalers[0]) / scalers[1]
            prediction = np.array(dnn_model.predict(df_i)).ravel()
            df.loc[eval_filter, score_name] = np.arctanh((prediction))
    return df[score_name]


def bdt_evaluation(df, variation, model, parameters):
    if parameters["do_massscan"]:
        mass_shift = parameters["mass"] - 125.0
    features = prepare_features(df, parameters, variation, add_year=False)
    score_name = f"score_{model} {variation}"
    try:
        df = df.compute()
    except Exception:
        pass
    df.loc[:, score_name] = 0
    if df.shape[0] == 0:
        return df[score_name]
    nfolds = 4
    for i in range(nfolds):
        # FIXME
        label = f"2016_jul7_{i}"

        # train_folds = [(i + f) % nfolds for f in [0, 1]]
        # val_folds = [(i + f) % nfolds for f in [2]]
        eval_folds = [(i + f) % nfolds for f in [3]]

        eval_filter = df.event.mod(nfolds).isin(eval_folds)
        scalers_path = f"{parameters['models_path']}/{model}/scalers_{label}.npy"
        scalers = np.load(scalers_path)
        model_path = (
            f"{parameters['models_path']}/{model}/" f"BDT_model_earlystop50_{label}.pkl"
        )

        bdt_model = pickle.load(open(model_path, "rb"))
        df_i = df[eval_filter]
        if df_i.shape[0] == 0:
            continue
        df_i.loc[df_i.region != "h-peak", "dimuon_mass"] = 125.0
        if parameters["do_massscan"]:
            df_i.loc[:, "dimuon_mass"] = df_i["dimuon_mass"] - mass_shift
        df_i = (df_i[features] - scalers[0]) / scalers[1]
        if len(df_i) > 0:
            if "multiclass" in model:
                prediction = np.array(
                    bdt_model.predict_proba(df_i.values)[:, 5]
                ).ravel()
            else:
                prediction = np.array(
                    bdt_model.predict_proba(df_i.values)[:, 1]
                ).ravel()
            df.loc[eval_filter, score_name] = np.arctanh((prediction))
    return df[score_name]


def get_variation(wgt_variation, sys_variation):
    if "nominal" in wgt_variation:
        if "nominal" in sys_variation:
            return "nominal"
        else:
            return sys_variation
    else:
        if "nominal" in sys_variation:
            return wgt_variation
        else:
            return None


def histogram(args, df=pd.DataFrame(), parameters={}):
    year = args["year"]
    var_name = args["var_name"]
    dataset = args["dataset"]
    if var_name in variables_lookup.keys():
        var = variables_lookup[var_name]
    else:
        var = Variable(var_name, var_name, 50, 0, 5)

    regions = parameters["regions"]
    channels = parameters["channels"]
    wgt_variations = [w for w in df.columns if ("wgt_" in w)]
    syst_variations = parameters["syst_variations"]

    variations = []
    for w in wgt_variations:
        for v in syst_variations:
            variation = get_variation(w, v)
            if variation:
                variations.append(variation)

    regions = [r for r in regions if r in df.region.unique()]
    channels = [c for c in channels if c in df["channel nominal"].unique()]

    # sometimes different years have different binnings (MVA score)
    if "score" in var.name:
        bins = parameters["mva_bins"][var.name.replace("score_", "")][f"{year}"]
        hist = (
            Hist.new.StrCat(regions, name="region")
            .StrCat(channels, name="channel")
            .StrCat(variations, name="variation")
            .StrCat(["value", "sumw2"], name="val_err")
            .Var(bins, name=var.name)
            .Double()
        )
    else:
        hist = (
            Hist.new.StrCat(regions, name="region")
            .StrCat(channels, name="channel")
            .StrCat(variations, name="variation")
            .StrCat(["value", "sumw2"], name="val_sumw2")
            .Reg(var.nbins, var.xmin, var.xmax, name=var.name, label=var.caption)
            .Double()
        )

    for region in regions:
        for w in wgt_variations:
            for v in syst_variations:
                variation = get_variation(w, v)
                if not variation:
                    continue
                var_name = f"{var.name} {v}"
                if var_name not in df.columns:
                    if var.name in df.columns:
                        var_name = var.name
                    else:
                        continue
                for channel in channels:
                    slicer = (
                        (df.dataset == dataset)
                        & (df.region == region)
                        & (df.year == year)
                        & (df[f"channel {v}"] == channel)
                    )
                    data = df.loc[slicer, var_name]
                    weight = df.loc[slicer, w]
                    hist.fill(region, channel, variation, "value", data, weight=weight)
                    hist.fill(
                        region,
                        channel,
                        variation,
                        "sumw2",
                        data,
                        weight=weight * weight,
                    )
                    # TODO: add treatment of PDF systematics
                    # (MC replicas)
    if parameters["save_hists"]:
        save_histogram(hist, var.name, dataset, year, parameters)
    hist_row = pd.DataFrame(
        [{"year": year, "var_name": var.name, "dataset": dataset, "hist": hist}]
    )
    return hist_row
