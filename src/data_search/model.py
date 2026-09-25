"""Pinned, local-only BGE encoder. Heavy libraries are imported only in the worker."""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path

from .config import atomic_json

MODEL_REPO = "Xenova/bge-small-zh-v1.5"
MODEL_REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
MODEL_ID = f"{MODEL_REPO}@{MODEL_REVISION}:cls-l2-512:qint8"
ASSETS = {"model.onnx": "onnx/model_quantized.onnx", "tokenizer.json": "tokenizer.json", "config.json": "config.json"}
SHA256 = {
    'model.onnx': '15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc',
    'tokenizer.json': '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26',
    'config.json': 'd4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f',
}


class ModelCancelled(RuntimeError):
    pass


def asset_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def download_model(directory: str, *, progress=None, cancelled=None, source_directory: str | None = None) -> dict:
    """Install pinned assets with bounded memory; completed assets survive a retry."""
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    source = Path(source_directory).expanduser().resolve() if source_directory else None
    if source:
        supplied = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        if supplied.get('model_id') != MODEL_ID:
            raise ValueError('Offline model fingerprint does not match the supported model')
    def check_cancel():
        if cancelled and cancelled():
            raise ModelCancelled('Model installation cancelled')
    def report(**fields):
        if progress:
            progress(fields)
    hashes = {}
    for number, (name, remote) in enumerate(ASSETS.items()):
        check_cancel()
        path = dest / name
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{remote}"
        report(asset=name, asset_index=number + 1, assets_total=len(ASSETS), phase='verifying', bytes_done=0, bytes_total=None)
        # Validate completed assets even when a prior job ended before manifest publication.
        if path.is_file():
            digest = asset_digest(path)
            if SHA256[name] == digest:
                hashes[name] = digest
                continue
        tmp = path.with_suffix(".download")
        digest = hashlib.sha256()
        check_cancel()
        response = (source / name).open('rb') if source else urllib.request.urlopen(url, timeout=15)
        with response, tmp.open("wb") as out:
            total = (source / name).stat().st_size if source else response.headers.get('Content-Length')
            total = int(total) if total and str(total).isdigit() else None
            done = 0
            report(phase='importing' if source else 'downloading', bytes_done=0, bytes_total=total)
            while block := response.read(1024 * 1024):
                check_cancel()
                digest.update(block)
                out.write(block)
                done += len(block)
                report(bytes_done=done, bytes_total=total)
            out.flush()
            os.fsync(out.fileno())
        check_cancel()
        if digest.hexdigest() != SHA256[name]:
            tmp.unlink(missing_ok=True)
            raise ValueError('Model asset checksum mismatch')
        os.replace(tmp, path)
        hashes[name] = digest.hexdigest()
    metadata = {"model_id": MODEL_ID, "repository": MODEL_REPO, "revision": MODEL_REVISION,
                "sha256": hashes, "license": "MIT", "dimensions": 512}
    atomic_json(dest / "manifest.json", metadata)
    return metadata


def model_ready(directory: str) -> bool:
    root = Path(directory)
    try:
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        return (manifest.get('model_id') == MODEL_ID and manifest.get('sha256') == SHA256
                and all((root / name).is_file() and (root / name).stat().st_size > 0 for name in ASSETS))
    except (OSError, ValueError, TypeError):
        return False


class Encoder:
    def __init__(self, directory: str, threads: int):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        root = Path(directory)
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("model_id") != MODEL_ID:
            raise ValueError("unsupported model fingerprint; rebuild indexes when changing model")
        for name, expected in SHA256.items():
            if asset_digest(root / name) != expected:
                raise ValueError("model asset checksum mismatch")
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = int(threads)
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.session = ort.InferenceSession(str(root / "model.onnx"), sess_options=options,
                                           providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: list[str], query: bool = False) -> list[list[float]]:
        if query:
            texts = ["为这个句子生成表示以用于检索相关文章：" + text for text in texts]
        encoded = self.tokenizer.encode_batch(texts)
        np = self.np
        values = {"input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                  "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
                  "token_type_ids": np.array([e.type_ids for e in encoded], dtype=np.int64)}
        output = self.session.run(None, {k: v for k, v in values.items() if k in self.inputs})[0]
        vectors = output[:, 0, :] if output.ndim == 3 else output
        vectors = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        return vectors.astype(np.float32).tolist()
