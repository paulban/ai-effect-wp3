"""
This module provides a simple FastAPI-based API for training and generating synthetic time series data
using Gretel's DoppelGANger model (https://github.com/gretelai/gretel-synthetics).

It is designed to have as few dependencies as possible, and with sintax compatible with old Python versions (tested with Python 3.9).

The API has two endpoints:

- `/train`: Trains a DoppelGANger model based on the provided specifications.
- `/generate`: Generates synthetic time series data using a trained model.

"""

import json
import logging
import os
import time
from typing import Dict
import pandas as pd
import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import PlainTextResponse
from fastapi import status
import zipfile
import io

from dgan import generate, load_train_result, save_train_result, train

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

app = FastAPI(
    title="Synthetic Data API",
    description="API for training and generating synthetic time series data using Gretel's DoppelGANger model.",
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
)

# --- AI-Effect Control Interface ---
try:
    from common import create_control_router, synthetic_data_handlers

    app.include_router(
        create_control_router(synthetic_data_handlers),
        prefix="/control",
    )
except ImportError as e:
    import logging as _logging
    _logging.warning(f"AI-Effect control interface not available: {e}")

model_Root = "models"


@app.post("/train", tags=["Synthetic Data Generation"])
async def train_new_model(
    uploaded_file: UploadFile = File(..., description="CSV file for training data"),
    user_id: str = Query("test_user_id", description="The id of the user"),
    model_name: str = Query("test_model", description="The name of the model"),
    index_col: str = Query("datetime", description="The name of the index column"),
    overwrite: bool = Query(False, description="Whether to overwrite existing model"),
    sequence_len: int = Query(100, description="Sequence length for training"),
    batch_size: int = Query(1000, description="Batch size for training"),
    epochs: int = Query(10, description="Number of epochs for training"),
    # kwargs: Optional[Dict[str, Any]] = None, # TODO Incorporate kwargs
) -> PlainTextResponse:
    """
    Endpoint to train the DGAN model with data from a CSV file.
    """

    await uploaded_file.seek(0)
    if uploaded_file.content_type != "text/csv":
        raise HTTPException(
            status_code=400, detail="Invalid file type. Please upload a CSV file."
        )

    train_df = pd.read_csv(uploaded_file.file)

    folder_path = os.path.join(model_Root, user_id, model_name)

    if os.path.exists(folder_path) and not overwrite:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Model {model_name} already exists and overwrite is set to False",
        )

    def _progress_callback(progress_info):
        info = {
            "epoch": progress_info.epoch + 1,
            "total_epochs": progress_info.total_epochs,
            "batch": progress_info.batch + 1,
            "total_batches": progress_info.total_batches,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
        logger.info(info)

        try:
            os.makedirs(folder_path, exist_ok=True)
            with open(
                os.path.join(folder_path, f"{model_name}_progress.json"), "w"
            ) as f:
                json.dump(info, f)
        except Exception as e:
            logger.exception(e)

    logger.info("About to train model")

    try:
        train_result = train(
            train_df,
            index_col=index_col,
            sequence_len=sequence_len,
            batch_size=batch_size,
            epochs=epochs,
            progress_callback=_progress_callback,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error training model: {e}",
        )

    logger.info("Saving model")

    save_train_result(train_result, folder_path)

    return PlainTextResponse(f"Model {model_name} trained successfully.")


@app.get("/generate", tags=["Synthetic Data Generation"])
def generate_synthetic_data(
    model_name: str = Query("test_model", description="The name of the model"),
    user_id: str = Query("test_user_id", description="The id of the user"),
    number_of_examples: int = Query(1, description="Number of examples to generate"),
    return_csv: bool = Query(True, description="Whether to return csv"),
):
    """
    Endpoint to generate synthetic data using the trained DGAN model.
    """
    logger.info(
        f"generate_synthetic_data called with model_name: {model_name} and user_id: {user_id}"
    )

    folder_path = os.path.join(model_Root, user_id, model_name)

    try:
        train_result = load_train_result(folder_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_name} not found for user {user_id}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error loading model: {e}",
        )

    try:
        synthetic_dfs = generate(train_result, num_examples=number_of_examples)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating synthetic data: {e}",
        )

    logger.debug(f"Returning synthetic data for model {model_name} and user {user_id}")

    if return_csv:
        for i, synthetic_df in enumerate(synthetic_dfs):
            synthetic_df["example"] = i + 1

        synthetic_dfs = pd.concat(synthetic_dfs)

        return Response(
            synthetic_dfs.to_csv(index=False),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=synthetic_data.csv"},
        )
    else:
        zip_io = io.BytesIO()
        with zipfile.ZipFile(
            zip_io, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as temp_zip:
            for i, synthetic_df in enumerate(synthetic_dfs):
                zip_path = f"synthetic_data_{i}.parquet"
                temp_zip.writestr(zip_path, synthetic_df.to_parquet(index=False))
        return Response(
            zip_io.getvalue(),
            media_type="application/x-zip-compressed",
            headers={"Content-Disposition": "attachment; filename=examples.zip"},
        )


@app.get("/models", tags=["Synthetic Data Generation"])
async def list_available_trained_models(
    user_id: str = Query("test_user_id", description="The id of the user"),
):
    user_models_dir = os.path.join(model_Root, user_id)

    available_models = []

    if os.path.exists(user_models_dir):
        for filename in os.listdir(user_models_dir):
            model_path = os.path.join(user_models_dir, filename, "model.pt")
            logger.info(model_path)
            if os.path.exists(model_path):
                available_models.append(
                    {
                        "name": filename,
                        # "features": specifications.feature_descriptions,
                        # "attributes": specifications.attribute_descriptions,
                    }
                )

    return available_models


@app.get("/training_info", tags=["Synthetic Data Generation"])
async def get_training_info(
    user_id: str = Query("test_user_id", description="The id of the user"),
    model_name: str = Query("test_model", description="The name of the model"),
) -> Dict:
    folder_path = os.path.join(model_Root, user_id, model_name)
    training_info_path = os.path.join(folder_path, f"{model_name}_progress.json")

    if not os.path.exists(training_info_path):
        return {"message": f"No training info found for model '{model_name}'"}

    with open(training_info_path, "r") as f:
        training_info = json.load(f)
    return training_info


if __name__ == "__main__":
    # train(
    #     specifications=examples_Of_Train_Specifications[0],
    #     username="test_user",
    #     model_name="test_model",
    #     batch_size=1000,
    #     epochs=10,
    # )

    # r = generate(
    #     model_name="test_model",
    #     username="test_user",
    #     number_of_examples=3,
    # )
    # print(r)
    ml_gretel_port_str = os.getenv("ML_GRETEL_PORT", 600)
    ml_gretel_reload_str = os.getenv("ML_GRETEL_RELOAD", "True")
    ml_gretel_reload = True if ml_gretel_reload_str.lower() == "true" else False

    try:
        ml_Gretel_Port = int(ml_gretel_port_str)
    except ValueError:
        logger.exception(
            f"ML_GRETEL_PORT must be an integer (was string = {ml_gretel_port_str}). Using default value of 42"
        )
        ml_Gretel_Port = 600

    print(f"Go to http://127.0.0.1:{ml_Gretel_Port}/docs")
    uvicorn.run(
        "main:app", host="0.0.0.0", port=ml_Gretel_Port, reload=ml_gretel_reload
    )
