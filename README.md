# Modal Vision & Inference System

High-performance GPU-accelerated ecosystem for computer vision and LLM inference on Modal.

## 1. The Vision Pillar (BioCLIP + SAM)
- **Model:** `imageomics/bioclip-2` (TreeOfLife-200M).
- **Capability:** State-of-the-art species/taxon classification.
- **Segmentation:** On-demand adaptive segmentation using SAM 2.1.

## 2. The Inference Pillar (Llama + Ollama)
- **Serving:** Llama-Router and Ollama integration for open-weight LLMs.
- **Registry:** `models.json` driven model loading.
- **Scale-to-Zero:** Cost-optimized GPU utilization.

## Ecosystem Vision
This system completes the "Sovereign AI" starter pack alongside the Modal Embedding Server.
- **Embedding:** Cloud-powered vector generation.
- **Inference:** LLM reasoning and generation.
- **Vision:** Deep classification and segmentation.