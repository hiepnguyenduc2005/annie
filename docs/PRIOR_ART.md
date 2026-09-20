# Prior art and reuse

Primary sources checked on 2026-09-19. Research results belong to their authors;
they do not establish Annie's accuracy, novelty, or hardware readiness.

| Source | Contribution | Annie use |
| --- | --- | --- |
| [NVIDIA ReMEmbR](https://github.com/NVIDIA-AI-IOT/remembr) | Long-horizon robot memory binding visual observations to space and time. | Design reference for timestamped captions and observer pose. Annie implements bounded SQLite retrieval; it does not run ReMEmbR's Milvus/agent stack or copy its code. |
| [Embodied-RAG](https://arxiv.org/abs/2409.18313) | Semantic organization of embodied memory for retrieval and generation. | Reference; its semantic forest is not implemented. |
| [Meta-Memory](https://arxiv.org/abs/2509.20754) | Retrieval and integration of semantic-spatial memories. | Reference; no reproduced comparative benchmark claim. |
| [Enter the Mind Palace](https://proceedings.mlr.press/v305/ginting25a.html) | Episodic scene-graph memories supporting long-term active embodied QA. | Reference. The final publication is CoRL 2025; an [RSS workshop version](https://rss25-roboreps.github.io/papers/25_Enter_the_Mind_Palace_Reaso.pdf) also exists. Annie does not claim persistent entity graphs. |
| [KARMA](https://arxiv.org/abs/2409.14908) | Long- and short-term memory for embodied agents. | Reference; not integrated. |
| [Semantic Flip](https://arxiv.org/abs/2606.16898), [code](https://github.com/ndb796/SemanticFlip) | Learning to reject questions unsupported by visual memory. | Refusal principle informs tests; the rejection model is not integrated. Correction: this paper is June **2026**, not 2025. |
| [Go2 Pro eldercare via WebRTC](https://link.springer.com/chapter/10.1007/978-3-032-29254-4_11) | Decoupled home-assistance architecture with edge perception. | Cite as related work. It does not prove our GX10/Go2 connection. |
| [Assistive elderly-care person following](https://www.mdpi.com/1424-8220/26/10/3263) | Vision-based following on a Unitree quadruped, Sensors 2026, 26(10), 3263. | Reference. The native-follow comparison is qualitative preliminary testing; do not turn it into a universal vendor-safety claim. |
| [DimOS](https://github.com/dimensionalOS/dimos) | Robot runtime, perception, navigation and simulator building blocks. | Trained Go1 policy and matched model reused; source pins and adaptation in [locomotion notes](../robot/simulation/LOCOMOTION.md). Full SDK integration remains separate. |
| [unitree_webrtc_connect](https://github.com/legion1581/unitree_webrtc_connect) | Community Unitree WebRTC driver. | Hardware integration candidate; not connected in the localhost demo. |
| [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) | Pretrained object/person detection. | YOLO11s runs on actual simulator-camera pixels for the person-stop interlock. Weights cached locally; source and license in [local perception](LOCAL_PERCEPTION.md). |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Local Whisper inference. | CPU/int8 tiny.en transcription of synthetic WAVs; [measurements](LOCAL_STT.md). |

[awesome-embodied-agents](https://github.com/Neal2020GitHub/awesome-embodied-agents)
is a discovery index, not evidence for individual performance claims. Guide-dog,
ROS 2, custom SLAM, and arm work remain outside the current assistance loop.
No sensor-spec numbers are needed for the current software claims.

- Three.js r128 (`robot/three.min.js`, vendored 2026-09-20 from
  https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js, sha256
  9274bbcec8d96168…, MIT licence, Copyright 2010-2021 Three.js Authors): offline
  fallback for the 4D space-time viewer (`robot/spacetime_viewer.html`).
