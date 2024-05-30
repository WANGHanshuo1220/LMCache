import torch
import time
import os
import hashlib
from typing import Tuple, List, Union, Iterator
import logging

logging.basicConfig(format='\033[33m%(levelname)s LMCache: \033[0m%(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# FIXME: currently is v0.1: store the kv cache in CPU memory in a dictionary

# TODO: (performance) don't do anything about the tensor if the key is already in the cache 
# TODO: (functionality) store to redis
# TODO: (functionality) configuration class
# TODO: (functionality) the model name should also be the key
# TODO: (functionality) the chunk size should also be related to the key
# TODO: (usability) global getter for the LMCacheEngine object

KVCache = Tuple[Tuple[torch.Tensor, torch.Tensor], ...]
class LMCacheEngine:
    def __init__(self, chunk_size: int = 256, persist_path: str = None):
        # TODO: remove persist_path in the future
        self.chunk_size = chunk_size
        self.persist_path = persist_path
        self.dict = {}
        if persist_path is not None and os.path.isfile(persist_path):
            logger.info(f"Found persisted file at {persist_path}, loading it right now...")
            self.dict = torch.load(persist_path)
            logger.info(f"Loaded {len(self.dict)} chunks")

    def _num_tokens_in_kv(
            self,
            kv_tensors: KVCache,
            fmt: str
    ) -> int:
        if fmt == "huggingface":
            return kv_tensors[0][0].shape[1]
        elif fmt == "vllm":
            return kv_tensors[0][0].shape[0]
        else:
            raise ValueError(f"Invalid format: {fmt}")

    def _get_init_hash(self) -> str:
        return ""

    def _hash(self, tokens: torch.Tensor, prefix_hash: str) -> str:
        # TODO: change it to a more efficient hash function
        return hashlib.sha256(prefix_hash.encode("ascii") + tokens.numpy().tobytes()).hexdigest()

    def _chunk_tokens(self, tokens: torch.Tensor, device) -> Iterator[torch.Tensor]:
        """
        Chunk the tokens into chunks of size self.chunk_size.
        
        Input:
            tokens: the input tokens, with shape [seq_len]
            device: the target device after chunking

        Output:
            a generator of chunks of tokens, each with shape [chunk_size]
        """
        for i in range(0, len(tokens), self.chunk_size):
            yield tokens[i:i+self.chunk_size].cpu()

    def _prefix_hash(
            self, 
            token_chunks: Iterator[torch.Tensor]
    ) -> Iterator[str]:
        prefix_hash = self._get_init_hash()
        for token_chunk in token_chunks:
            prefix_hash = self._hash(token_chunk, prefix_hash)
            yield prefix_hash

    def _slice_kv_at(
            self,
            start_idx: int,
            end_idx: int,
            kv_tensors: KVCache,
            fmt: str,
            device) -> KVCache:
        """
        Slice the kv cache of tokens between [start_idx:end_idx]
        """
        match fmt:
            case "huggingface":
                return tuple((kv[0][:, start_idx:end_idx, :].to(device), 
                              kv[1][:, start_idx:end_idx, :].to(device)) 
                             for kv in kv_tensors)
            case "vllm":
                return tuple((kv[0][start_idx:end_idx, :, :].to(device),
                              kv[1][start_idx:end_idx, :, :].to(device))
                             for kv in kv_tensors)
            case _:
                raise ValueError(f"Invalid format: {fmt}")

    def _chunk_kv(self, 
                  kv_tensors: KVCache,
                  fmt: str,
                  device) -> Iterator[KVCache]:
        """
        Chunk the kv cache into chunks of size self.chunk_size.

        Input:
            tokens: the input tokens, with shape [seq_len]
            kv_tensors: the kv cache of the tokens, in the format of nested tuples
            fmt: either 'huggingface' or 'vllm'

        Output:
            a generator of tuples, each tuple is a chunk of tokens and the corresponding kv cache.
        """
        num_tokens = self._num_tokens_in_kv(kv_tensors, fmt)

        for i in range(0, num_tokens, self.chunk_size):
            yield self._slice_kv_at(i, i+self.chunk_size, kv_tensors, fmt, device)

    def _make_chunks_skip_exsiting(
            self, 
            tokens: torch.Tensor,
            kv_tensors: KVCache,
            fmt: str,
            device
    ) -> Iterator[Tuple[torch.Tensor, KVCache]]:
        """
        Skip the existing chunks and return the rest of the chunks
        """
        chunk_hashes = self._prefix_hash(self._chunk_tokens(tokens, device))
        num_tokens = self._num_tokens_in_kv(kv_tensors, fmt)

        for chunk_hash, idx in zip(chunk_hashes, range(0, num_tokens, self.chunk_size)):
            if (chunk_hash, fmt) not in self.dict:
                yield chunk_hash, self._slice_kv_at(idx, idx+self.chunk_size, kv_tensors, fmt, device)


    def _make_chunks(
            self, 
            tokens: torch.Tensor,
            kv_tensors: KVCache,
            fmt: str,
            device = 'cpu',
            skip_existing = True,
    ) -> Iterator[Tuple[torch.Tensor, KVCache]]:
        """
        Returns a generator of zipped (chunk_hash, chunk_kv) tuples
        """
        if skip_existing:
            return self._make_chunks_skip_exsiting(tokens, kv_tensors, fmt, device)
        else:
            return zip(self._prefix_hash(self._chunk_tokens(tokens, device)), self._chunk_kv(kv_tensors, fmt, device))

    def _concat_kv_chunks(
            self,
            kv_chunks: List[KVCache],
            dim: int,
            fmt: str,
            device,
    ) -> KVCache:
        for kv_layer in zip(*kv_chunks):
            klist, vlist = zip(*kv_layer)
            klayer = torch.cat(klist, dim=dim).to(device)
            vlayer = torch.cat(vlist, dim=dim).to(device)
            yield (klayer, vlayer)

    def store(
            self, 
            tokens: torch.Tensor,
            kv_tensors: KVCache,
            fmt: str,
            skip_existing = True,
    ) -> None:
        """
        Store the KV cache of the tokens into the cache engine.

        Input:
            tokens: the input tokens, with shape [seq_len]
            kv_tensors: the kv cache of the tokens, in the format of nested tuples
            format: either 'huggingface' or 'vllm'
                    For huggingface, it should have the shape of [num_heads, num_tokens, head_size]
                    For vllm, it should have the shape of [num_tokens, num_heads, head_size]

        Returns:
            None

        Note:
            The KV cache should NOT have the "batch" dimension.
        """
        assert len(tokens.shape) == 1, f"Invalid shape of tokens: {tokens.shape}"
        assert len(kv_tensors) > 0, "Empty kv_tensors"
        assert len(tokens) == self._num_tokens_in_kv(kv_tensors, fmt), "Number of tokens in the kv cache does not match the input tokens"

        # TODO: check shapes

        ''' chunk the tokens and the kv caches '''
        chunk_hashes_and_kvs = self._make_chunks(tokens, kv_tensors, fmt, device='cpu', skip_existing=skip_existing)

        ''' store them into the dictionary '''
        n_chunks = 0
        for chunk_hash, kv_chunk in chunk_hashes_and_kvs:
            self.dict[(chunk_hash, fmt)] = kv_chunk
            n_chunks += 1

        logger.info(f"Stored/updated {n_chunks} chunks. Currently {len(self.dict)} chunks in the cache")


    def retrive(self,
                tokens: torch.Tensor,
                fmt: str,
                device: str = 'cuda'
                ) -> Tuple[KVCache, int]:
        """
        Retrive the KV cache of the tokens from the cache engine. The retrived KV cache 
        should be a prefix of the input tokens.

        Input:
            tokens: the input tokens, with shape [seq_len]
            format: either 'huggingface' or 'vllm'
                    For huggingface, it should have the shape of [num_heads, num_tokens, head_size]
                    For vllm, it should have the shape of [num_tokens, num_heads, head_size]

        Output: 
            kv_tensors: the kv cache of the tokens, in the format of nested tuples
            num_tokens: the number of tokens in the kv cache
        """
        st = time.perf_counter()
        chunk_hashes = self._prefix_hash(self._chunk_tokens(tokens, device='cpu'))
        retrived_kv_chunks: List[KVCache] = []

        ''' retrive the kv cache '''
        for chunk_hash in chunk_hashes:
            if (chunk_hash, fmt) in self.dict:
                retrived_kv_chunks.append(self.dict[(chunk_hash, fmt)])
            else:
                break

        ''' concatenate the kv cache '''
        dim = None
        match fmt:
            case "huggingface":
                dim = 1
            case 'vllm':
                dim = 0
            case _:
                raise ValueError(f"Invalid format: {fmt}")

        st2 = time.perf_counter()
        ret = tuple(self._concat_kv_chunks(retrived_kv_chunks, dim, fmt, device))
        ed2 = time.perf_counter()
        logger.info(f"Concatenated {len(retrived_kv_chunks)} chunks -- elapsed time {ed2 - st2}")
        retrived_token_count = 0 if len(ret) == 0 else ret[0][0].shape[dim]
        ed = time.perf_counter()
        logger.info(f"Retrived {len(retrived_kv_chunks)} chunks ({retrived_token_count} tokens in total) -- elapsed time {ed - st}")
        return ret, retrived_token_count

    def persist(self):
        """
        Temporary function of persisting
        """
        if self.persist_path is not None:
            torch.save(self.dict, self.persist_path)
            logger.info(f"Persisted the cache to {self.persist_path}. {os.path.getsize(self.persist_path) / 1e6} MBytes in total")
        else:
            raise RuntimeError("Persist path not found, please set self.persist_path")
