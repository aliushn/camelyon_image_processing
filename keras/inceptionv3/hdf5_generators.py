#!/usr/bin/env python3

# Necessary for graphs on headless server
import matplotlib
matplotlib.use("Agg")

# Other imports
import tensorflow.keras as keras
from tensorflow.keras.applications.inception_v3 import InceptionV3
from tensorflow.keras.layers import Dense, GlobalAveragePooling2D, Dropout
from tensorflow.keras.models import Model, load_model
from tensorflow.keras.utils import multi_gpu_model, to_categorical, HDF5Matrix
from tensorflow.keras.callbacks import ModelCheckpoint
import tensorflow as tf
import argparse
import numpy as np
import matplotlib.pyplot as plt


def get_arguments():
    parser = argparse.ArgumentParser(
        description="Keras InceptionV3 architecture for tumor image classification"
    )
    # Multi-GPU
    parser.add_argument(
        "-g", "--GPUs", help="Number of GPUs to run on (must be >= 1)",
        required=False, type=int, default=1
    )
    # Input weights for checkpointing
    parser.add_argument(
        "-m", "--input_model", help="Input model with weights to use for training",
        required=False, type=str
    )
    # Training Parameters
    parser.add_argument(
        "-e", "--epochs", help="number of epochs to use in training",
        required=True, type=int
    )
    parser.add_argument(
        "-b", "--batch_size", help="size of each batch in minibatch sampling",
        required=True, type=int
    )
    parser.add_argument(
        "-t", "--tiles", help="number of tiles from the HDF5 file to use for \
        training/validation, (20% validation split)",
        required=False, type=int
    )
    # Input/Output Paths
    parser.add_argument(
        "-f", "--file_input", help="path to hdf5 file with training and \
        validation data", required=True, type=str
    )
    parser.add_argument(
        "-o", "--output_directory", help="Directory to save weights and \
        training graphical summary, current directory by default",
        required=False, default=".", type=str
    )
    parser.add_argument(
        "-n", "--output_name", help="Name of output files",
        required=False, default="", type=str
    )
    # Optional extra output, model is saved every epoch by default
    parser.add_argument(
        "-H", "--graphical_history", help="Saves a graphical summary of \
        the training history to the output directory.", required=False,
        action="store_true"
    )
    return parser.parse_args()


args = get_arguments()
# Strip trailing directory slashes
args.output_directory = args.output_directory.rstrip("/")

hdf5_path = args.file_input

# Load in data from HDF5 file
train_data = HDF5Matrix(hdf5_path, "train_img")
train_labels = HDF5Matrix(hdf5_path, "train_labels")
train_labels = to_categorical(train_labels)
val_data = HDF5Matrix(hdf5_path, "val_img")
val_labels = HDF5Matrix(hdf5_path, "val_labels")
val_labels = to_categorical(val_labels)

# Optionally subset data to train on fewer tiles
if(args.tiles is not None):
    train_frac = int(args.tiles*0.8)
    val_frac = int(args.tiles*0.2)
    train_data = HDF5Matrix(hdf5_path, "train_img", end=train_frac)
    train_labels = HDF5Matrix(hdf5_path, "train_labels", end=train_frac)
    train_labels = to_categorical(train_labels)
    val_data = HDF5Matrix(hdf5_path, "val_img", end=val_frac)
    val_labels = HDF5Matrix(hdf5_path, "val_labels", end=val_frac)
    val_labels = to_categorical(val_labels)

new_model = False

# Input model provided
if(args.input_model):
    input_model = load_model(args.input_model)
    if(args.GPUs <= 1):
        model = Model(inputs=input_model.input, outputs=input_model.output)
    if(args.GPUs > 1):
        with tf.device("/cpu:0"):
            partial_model = Model(inputs=input_model.input, outputs=input_model.output)
        model = multi_gpu_model(partial_model, gpus=args.GPUs)

# Otherwise, create model from scratch
else:
    new_model = True
    # Inception layers with imagenet weights
    base_model = InceptionV3(
        weights='imagenet',
        include_top=False,
        input_shape=(256, 256, 3)
    )
    # Output layers
    out = base_model.output
    out = GlobalAveragePooling2D()(out)
    out = Dense(1024, activation='relu')(out)
    out = Dropout(rate=0.2)(out)
    predictions = Dense(2, activation='softmax')(out)

    # Define Model via functional API
    # Single GPU, if args.GPUs == 0 CPU processing will be performed (discouraged)
    if(args.GPUs <= 1):
        model = Model(inputs=base_model.input, outputs=predictions)
    # Multi-GPU parallel processing
    elif(args.GPUs > 1):
        with tf.device("/cpu:0"):
            model = Model(inputs=base_model.input, outputs=predictions)
        model = multi_gpu_model(model, gpus=args.GPUs)

    print("Freezing Inception Weights")

    # Train for a few epochs with inception weights frozen on a fresh model
    for layer in model.layers[:280]:
        layer.trainable = False
    for layer in model.layers[280:]:
        layer.trainable = True

    model.compile(
        optimizer='sgd',
        loss='categorical_crossentropy',
        metrics=['accuracy']
    )

    model.fit(
        train_data,
        train_labels,
        epochs=1,
        batch_size=args.batch_size,
        shuffle='batch'
    )

    print("Unfreezing inception weights")
    for layer in model.layers[:280]:
        layer.trainable = True

    model.compile(
        optimizer='sgd',
        loss='categorical_crossentropy',
    )

    model.save("intermediate_model.hdf5")

# Load the intermediate model and resume training with inception weights unfrozen
if(new_model):
    epochs = max(1, args.epochs - 1)
    intermediate_model = load_model("intermediate_model.hdf5")
    if(args.GPUs <= 1):
        model = Model(inputs=intermediate_model.input, outputs=intermediate_model.output)
    if(args.GPUs > 1):
        with tf.device("/cpu:0"):
            partial_model = Model(inputs=intermediate_model.input,
                                  outputs=intermediate_model.output)
        model = multi_gpu_model(partial_model, gpus=args.GPUs)

else:
    epochs = args.epochs

# Compile
model.compile(
    optimizer='sgd',
    loss='categorical_crossentropy',
    metrics=['accuracy']
)

# Callbacks to save the model every epoch if it improves
checkpoint = keras.callbacks.ModelCheckpoint(
    args.output_directory + "/" + args.output_name + "_model.h5",
    monitor='val_acc',
    verbose=1,
    save_best_only=True,
    save_weights_only=False,
    mode='auto',
    period=1
)

callbacks_list = [checkpoint]

# Train
trained_model = model.fit(
    train_data,
    train_labels,
    validation_data=(val_data, val_labels),
    epochs=epochs,
    batch_size=args.batch_size,
    shuffle='batch',
    callbacks=callbacks_list
)

# early stopping callback - increase in validation loss?

if(args.graphical_history):
    history = trained_model.history
    arange = np.arange(1, len(history["loss"]) + 1)
    plt.style.use("ggplot")
    plt.figure()
    plt.plot(arange, history["loss"], label="training loss")
    plt.plot(arange, history["val_loss"], label="validation loss")
    plt.plot(arange, history["acc"], label="training accuracy")
    plt.plot(arange, history["val_acc"], label="validation accuracy")
    plt.title("Keras InceptionV3 Training History")
    plt.xlabel("Epoch Number")
    plt.ylabel("Loss / Accuracy")
    plt.legend()
    plt.savefig(args.output_directory + "/" + args.output_name + "_training_history.png")
    plt.close()
