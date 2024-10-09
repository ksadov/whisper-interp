import torch
import json
import os
import numpy as np
from torch.utils.data import Dataset, DataLoader
from typing import Optional

from src.dataset.audio import AudioDataset
from src.models.hooked_model import init_cache
from src.models.autoencoder import init_from_checkpoint

class FlyActivationDataloader(torch.utils.data.DataLoader):
    """
    Dataloader for computing Whisper or SAE activations on the fly
    """
    def __init__(self,  data_path: str, whisper_model: str, sae_checkpoint: Optional[str], 
                 layer_to_cache: str, device: torch.device, batch_size: int, dl_max_workers: int,
                 subset_size: Optional[int] = None):
        self.whisper_cache = init_cache(whisper_model, layer_to_cache, device)
        self.whisper_cache.model.eval()
        self.sae_model = init_from_checkpoint(sae_checkpoint, whisper_model, layer_to_cache) if sae_checkpoint else None
        self.dataset = AudioDataset(data_path, device)
        if subset_size:
            self.dataset = torch.utils.data.Subset(self.dataset, range(subset_size))
        dl_kwargs = {
            "batch_size": batch_size,
            "pin_memory": False,
            "drop_last": True,
            "num_workers": dl_max_workers,
        }
        self.dataloader = DataLoader(self.dataset, **dl_kwargs)
        self.activation_shape = self._get_activation_shape()
        assert self.sae_model is None or layer_to_cache == self.sae_model.hp['layer_name'], \
            "layer_to_cache must match the layer that the SAE model was trained on"
        assert self.sae_model is None or self.sae_model.hp['whisper_model'] == whisper_model, \
            "whisper_model must match the whisper_model that the SAE model was trained on"

    def _get_activation_shape(self):
        mels, _ = self.dataset[0]
        with torch.no_grad():
            self.whisper_cache.forward(mels)
            first_activation = self.whisper_cache.activations[0]
            if self.sae_model:
                _, c = self.sae_model(first_activation)
                return c.squeeze().shape
            else:
                return first_activation.squeeze().shape

    def __iter__(self):
        for batch in self.dataloader:
            self.whisper_cache.reset_state()
            mels, global_file_names = batch
            self.whisper_cache.forward(mels)
            activations = self.whisper_cache.activations
            if self.sae_model:
                _, c = self.sae_model(activations)
                yield c, global_file_names
            else:
                yield activations, global_file_names
    
    def __len__(self):
        return len(self.dataloader)
        

class MemoryMappedActivationsDataset(Dataset):
    """
    Dataset for activations stored in memory-mapped files geneerated by src.scripts.collect_activations
    """
    def __init__(self, data_dir, layer_name, max_size=None):
        self.data_dir = data_dir
        self.layer_name = layer_name
        self.metadata_file = os.path.join(data_dir, f"{layer_name}_metadata.json")
        self.tensor_file = os.path.join(data_dir, f"{layer_name}_tensors.npy")
        
        with open(self.metadata_file, 'r') as f:
            self.metadata = json.load(f)
        
        self.mmap = np.load(self.tensor_file, mmap_mode='r')
        if max_size is not None:
            self.metadata['filenames'] = self.metadata['filenames'][:max_size]
            self.metadata['tensor_shapes'] = self.metadata['tensor_shapes'][:max_size]
            self.mmap = self.mmap[:max_size]
        self.activation_shape = self._get_activation_shape()

    def _get_activation_shape(self):
        return self.metadata['tensor_shapes'][0]

    def __len__(self):
        return len(self.metadata['filenames'])
    
    def __getitem__(self, idx):
        filename = self.metadata['filenames'][idx]
        tensor_shape = self.metadata['tensor_shapes'][idx]
        
        # Get the flattened tensor data
        tensor_data = self.mmap[idx]
        
        # Reshape the tensor data to its original shape
        tensor = torch.from_numpy(tensor_data.reshape(tensor_shape))
        
        return tensor, filename
 