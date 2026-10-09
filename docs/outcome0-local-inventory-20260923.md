# Outcome 0 local asset checklist — 2026-09-23

**Result: not ready to run molecular distillation or scientific evaluation.**
This is a bounded read-only inventory of existing assets. The checklist is the
only inventory output added to the repository; no assets were downloaded,
installed, generated, executed, relabelled as evidence, or rights-approved.
No external service was called. Missing assets have no invented hash or result.

## Scope and evidence limits

- Inspected Tessera source, dependency declarations, relevant tests and contracts;
  searched repository filenames for model/data formats (`safetensors`, `pt`,
  `pth`, `ckpt`, `onnx`, `gguf`, `csv`, `parquet`, `sdf`, `mol`, `h5`, `hdf5`,
  `npz`, `npy`, `smi`, `smiles`, `mol2`, `cif`, `bin`, `pkl`, `pickle`). None
  matched. Filename discovery is not a byte-level audit of every ignored file.
- Bounded filename searches under `$HOME/Code`, `$HOME/Documents`, `$HOME/Desktop`,
  and `$HOME/Downloads` for MixFM/molecular/teacher/distillation names and common
  checkpoint/molecular formats found no additional in-scope research asset.
  Not all arbitrary filenames, archives, hidden paths, other virtual environments,
  cloud folders, external drives, or container filesystems were inspected.
  No unrelated personal document contents were read or hashed.
- Inspected the existing Hugging Face cache directory and its one model snapshot,
  plus top-level Keras/PyTorch cache entries. The PyTorch cache names unrelated
  Fashion-MNIST material; it was not adopted or validated for molecular work.
- Inspected installed distribution metadata in **Tessera's `.venv` only**, without
  importing ML packages or invoking model code. Other environments remain unknown.
- Read-only hardware/compute observations came from `uname`, `sysctl`,
  `system_profiler SPDisplaysDataType`, local Docker `info`, and the explicitly
  selected local Kubernetes node's allocatable resources. No benchmark or workload
  was launched.
- The [held-recovery baseline](post-mixfm-recovery-20260923.md) provides live
  metadata context. No synthetic/caller-declared digest below is substituted for
  the hash of an absent dataset, model, teacher output, or protected split.

## Readiness checklist

| Prerequisite | Observed assets / status | Missing acceptance evidence |
|---|---|---|
| Rights-cleared molecular data bytes | **Not found in inspected scope.** Tessera contains declarations and fake fixtures, not a validated molecular corpus. | Actual versioned bytes, source provenance and applicable license/terms; explicit permitted training, derivative/teacher-output and evaluation use; chemistry/unit/missingness/content validation. |
| Base/student model weights | **No weight shards found.** One cached LLaMA snapshot has only six auxiliary files (listed below). | Complete selected checkpoint and architecture/tokenizer compatibility, byte hashes/sizes, use/redistribution terms, reproducible load test on approved compute. |
| Teacher model and teacher artifacts | **Not found.** Cached tokenizer/model implementation is neither teacher weights nor teacher predictions. | Authorized teacher identity/weights or separately approved service; molecular inputs and outputs/logits/embeddings/uncertainties as required; prompts/procedure, seeds, provenance and hashes. No teacher call authorized by this inventory. |
| Molecular trainer/distillation code | **Not identified.** `outcome0_bootstrap.py` is an offline fake-effect bootstrap contract. | Actual data loader/featurization, selected distillation objectives, trainer, checkpointing, reproducibility/configuration and bounded run entrypoint. |
| Scientific evaluator | **Not identified.** `evaluation_models.py` validates caller-reported values and compares thresholds; it does not compute molecular predictions or metrics. | Executable evaluator with declared metrics, uncertainty/aggregation, baselines, independent ground truth, leakage checks and reproducible tests. |
| ML/chemistry dependencies | **Missing from Tessera `.venv`.** Existing lock is for control-plane development, not an ML environment. | Approved, reproducible scientific dependency/runtime lock and validated compatibility; no installation performed. |
| Compute | **Local CPU available; limited local graphics only.** See measured resources below. | Model-sized memory/VRAM feasibility, validated backend, bounded execution authority/resources/time, and any required isolated accelerator target. No available remote GPU established. |
| Protected train/validation/test inputs | **Not found.** Declared split digests and grouped split policies are metadata only. | Real versioned membership/labels, scaffold/group/domain leakage controls, protected evaluation access, and predeclared few-shot/holdout/scaffold-OOD/counterfactual/calibration/capability-retention inputs as applicable. |
| Scientific plan and success gates | **Planning/review metadata exists**, but live Outcome 0 says no dataset/model/metrics supplied. | Bind the actual data/code/environment/teacher/student identities and protected splits to the approved protocol; obtain appropriate scientific review. Operational UI/backup success is not a scientific go. |

### Existing fixtures and live metadata are not research inputs

`tests/test_outcome0_environment_bootstrap.py` defaults to the literal bytes
`b"fixture-data"`, a `fixture://.../train.json` URI, and placeholder package/image
digests. Those test values are not a downloaded molecular dataset or installed
RDKit runtime. `tests/test_dataset_ingestion.py` declares schema/license/split
metadata without supplying corresponding dataset bytes.

Live Outcome 0 study `study_ba00da5f49994ee18bd566a832552f9f` has an empty
dataset-snapshot list. Its stored evaluation explicitly reports missing dataset
bytes, trained model, execution and metrics; `execution_granted` is false and
`scientific_validity` is `not_assessed`. Outcome 1's smoke declarations include
zero-row records, zero checked rows, `custom_declared` licenses and
`content_validation: not_performed`; those are not rights-cleared bytes. The
source assessment explicitly supports only the local no-evidence statement.

## Present bytes: measured SHA-256 inventory

Hashes below were computed with `shasum -a 256` against files actually present on
2026-09-23. They identify bytes, not correctness, rights clearance, scientific
validity, a complete environment, or permission to execute code.

### Repository implementation and lock files

| Present path | SHA-256 | What it establishes |
|---|---|---|
| `pyproject.toml` | `a0d66bcb5afa94862b5e6572c9568c8bc318748f293d43ed7f53bc0d3b59ab8a` | Control-plane dependency declaration |
| `uv.lock` | `79576166e4ba0ba41106e7f5b2263887341ea9fba2c5f236cbdf6e1628b9ab10` | Existing dependency lock, not a molecular training lock |
| `src/tessera_provisioner/outcome0_bootstrap.py` | `cdde5c2e7496f972f364a58959b9198fe7bd3991467765d49285c4de2896c613` | Offline fake-effect bootstrap implementation |
| `src/tessera_control/evaluation_models.py` | `e24d62611ea3910bd72ea49fc1f31dd716c50dc9c62a97d8321ea0996f1b5cef` | Typed caller-reported observations / deterministic gate comparison |
| `src/tessera_control/dataset_ingestion.py` | `c8262410df35ed0d4032d53cef13b7f10b0fa6bcb89ffbbec09f0bd4cee36cf6` | Dataset metadata bookkeeping, not a molecular loader |
| `src/tessera_worker/salad_contract.py` | `7b78cc4f2fbc7163fd765c9f5e8a2ccb50dbb7f258d4700f93d4f9ffd0976ea1` | Fail-closed future provider contract, not live Salad compute |

### Existing Hugging Face auxiliary snapshot

Root: `~/.cache/huggingface/hub/models--togethercomputer--LLaMA-2-7B-32K/`
with snapshot `aef6d8946ae1015bdb65c478a2dd73b58daaef47`.
Its six referenced blobs total **546,867 bytes**. No weight shard, weight index,
teacher-output corpus or cached license document was found in this snapshot.
The config declares `LlamaForCausalLM`, float16 and 32 layers; that does not prove
the model can load. No cached model implementation was imported or executed.

| Present snapshot file | SHA-256 |
|---|---|
| `added_tokens.json` | `39c257654f85e478a9cb3bf34da53b3e4625e7b05ce15f193fe41724fae7b825` |
| `config.json` | `d2b0eff697536133da634be1648110cebfc02c6ad0347439e178dc0a610acc3c` |
| `modeling_flash_llama.py` | `88aff7693b6e3dec9e63b4de77803401cba76abe028374589cb14dfa288a977b` |
| `special_tokens_map.json` | `2ea88203b955648951a454e73a73967ff77ba4e79ae8ceb4400d4ab2cb040777` |
| `tokenizer_config.json` | `893015e6686bbf1ecbaef56f21fe9a0a5eba5d6700eb44322d767a5c2ffb9ba6` |
| `tokenizer.model` | `9e556afd44213b6bd1be2b850ebbbd98f5481437a8021afaf58ee7fb1818d347` |

## Dependencies and compute observed

None of these distributions was installed in Tessera `.venv`:
`torch`, `tensorflow`, `jax`, `jaxlib`, `mlx`, `numpy`, `scipy`, `pandas`,
`pyarrow`, `scikit-learn`, `rdkit`, `transformers`, `datasets`, `accelerate`,
`peft`, `safetensors`, `tokenizers`, `chemprop`, `deepchem`.
The project declares FastAPI, Pydantic, PyYAML and Uvicorn, with HTTPX, pytest
and Ruff for development. No ML/CUDA runtime compatibility was tested.

| Resource | Read-only observation | Qualification |
|---|---|---|
| Host | Intel `x86_64`, `MacBookPro15,1`, 6 physical / 12 logical CPUs, 34,359,738,368 bytes RAM (32 GiB) | No performance or free-memory guarantee |
| Host graphics | Intel UHD 630, 1536 MB dynamic VRAM; AMD Radeon Pro 560X, 4 GB VRAM | No NVIDIA CUDA GPU observed; Metal reporting is not validated ML acceleration |
| Local Docker | `desktop-linux`, Linux/x86_64, 12 CPUs, 8,321,867,776 bytes total memory | Shared VM resources, not a reservation |
| Kubernetes node | `desktop-control-plane`: allocatable CPU `12`, memory `8126824Ki`, ephemeral storage `61199172Ki`, Pods `110` | Allocatable is not idle capacity; no GPU resource advertised |
| Remote/Salad GPU | Not queried; no available target established | Existing contract is disabled/fail-closed, not an operational adapter |

## Required next inputs (no acquisition authorized here)

- [ ] Identify and supply rights-cleared molecular data bytes plus applicable terms
  and provenance; verify content/units/missingness before adopting a snapshot.
- [ ] Select and supply compatible student/base and teacher assets, licenses and
  complete byte manifests; specify whether teacher artifacts are precomputed.
- [ ] Supply or implement the actual molecular trainer and evaluator, with tests,
  versioned configs and a scientific environment lock.
- [ ] Supply protected split membership and ground-truth/evaluation inputs, plus
  contamination/leakage policy and access controls.
- [ ] Establish model-sized compute feasibility, isolation, authorization and
  bounded resource/time/spending limits before any execution.
- [ ] Bind real asset hashes and evaluation procedures to the program protocol;
  retain inconclusive/no-go status until missing evidence is actually obtained.

An asset might exist outside this bounded inspection. Supplying its explicit
local path and rights/provenance record is the next inventory action—not inventing
an entry, downloading a replacement, or treating a metadata digest as present bytes.
