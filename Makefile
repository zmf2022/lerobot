.PHONY: tests

PYTHON_PATH := $(shell which python)

# If Poetry is installed, redefine PYTHON_PATH to use the Poetry-managed Python
POETRY_CHECK := $(shell command -v poetry)
ifneq ($(POETRY_CHECK),)
	PYTHON_PATH := $(shell poetry run which python)
endif

export PATH := $(dir $(PYTHON_PATH)):$(PATH)

DEVICE ?= cpu

build-cpu:
	docker build -t lerobot:latest -f docker/lerobot-cpu/Dockerfile .

build-gpu:
	docker build -t lerobot:latest -f docker/lerobot-gpu/Dockerfile .

test-end-to-end:
	${MAKE} DEVICE=$(DEVICE) test-act-ete-train
	${MAKE} DEVICE=$(DEVICE) test-act-ete-train-resume
	${MAKE} DEVICE=$(DEVICE) test-act-ete-eval
	${MAKE} DEVICE=$(DEVICE) test-diffusion-ete-train
	${MAKE} DEVICE=$(DEVICE) test-diffusion-ete-eval
	${MAKE} DEVICE=$(DEVICE) test-tdmpc-ete-train
	${MAKE} DEVICE=$(DEVICE) test-tdmpc-ete-eval
	${MAKE} DEVICE=$(DEVICE) test-tdmpc-ete-train-with-online

test-act-ete-train:
	python lerobot/scripts/train.py \
		--policy.type=act \
		--policy.dim_model=64 \
		--policy.n_action_steps=20 \
		--policy.chunk_size=20 \
		--env.type=aloha \
		--env.episode_length=5 \
		--dataset.repo_id=lerobot/aloha_sim_transfer_cube_human \
		--dataset.image_transforms.enable=true \
		--dataset.episodes="[0]" \
		--batch_size=2 \
		--offline.steps=4 \
		--online.steps=0 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--save_freq=2 \
		--save_checkpoint=true \
		--log_freq=1 \
		--wandb.enable=false \
		--device=$(DEVICE) \
		--output_dir=tests/outputs/act/

test-act-ete-train-resume:
	python lerobot/scripts/train.py \
		--config_path=tests/outputs/act/checkpoints/000002/pretrained_model/train_config.json \
		--resume=true

test-act-ete-eval:
	python lerobot/scripts/eval.py \
		--policy.path=tests/outputs/act/checkpoints/000004/pretrained_model \
		--env.type=aloha \
		--env.episode_length=5 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--device=$(DEVICE)

test-diffusion-ete-train:
	python lerobot/scripts/train.py \
		--policy.type=diffusion \
		--policy.down_dims='[64,128,256]' \
		--policy.diffusion_step_embed_dim=32 \
		--policy.num_inference_steps=10 \
		--env.type=pusht \
		--env.episode_length=5 \
		--dataset.repo_id=lerobot/pusht \
		--dataset.image_transforms.enable=true \
		--dataset.episodes="[0]" \
		--batch_size=2 \
		--offline.steps=2 \
		--online.steps=0 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--save_checkpoint=true \
		--save_freq=2 \
		--log_freq=1 \
		--wandb.enable=false \
		--device=$(DEVICE) \
		--output_dir=tests/outputs/diffusion/

test-diffusion-ete-eval:
	python lerobot/scripts/eval.py \
		--policy.path=tests/outputs/diffusion/checkpoints/000002/pretrained_model \
		--env.type=pusht \
		--env.episode_length=5 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--device=$(DEVICE)

test-tdmpc-ete-train:
	python lerobot/scripts/train.py \
		--policy.type=tdmpc \
		--env.type=xarm \
		--env.task=XarmLift-v0 \
		--env.episode_length=5 \
		--dataset.repo_id=lerobot/xarm_lift_medium \
		--dataset.image_transforms.enable=true \
		--dataset.episodes="[0]" \
		--batch_size=2 \
		--offline.steps=2 \
		--online.steps=0 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--save_checkpoint=true \
		--save_freq=2 \
		--log_freq=1 \
		--wandb.enable=false \
		--device=$(DEVICE) \
		--output_dir=tests/outputs/tdmpc/

test-tdmpc-ete-eval:
	python lerobot/scripts/eval.py \
		--policy.path=tests/outputs/tdmpc/checkpoints/000002/pretrained_model \
		--env.type=xarm \
		--env.episode_length=5 \
		--env.task=XarmLift-v0 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--device=$(DEVICE)

test-tdmpc-ete-train-with-online:
	python lerobot/scripts/train.py \
		--policy.type=tdmpc \
		--env.type=pusht \
		--env.obs_type=environment_state_agent_pos \
		--env.episode_length=5 \
		--dataset.repo_id=lerobot/pusht_keypoints \
		--dataset.image_transforms.enable=true \
		--dataset.episodes="[0]" \
		--batch_size=2 \
		--offline.steps=2 \
		--online.steps=20 \
		--online.rollout_n_episodes=2 \
		--online.rollout_batch_size=2 \
		--online.steps_between_rollouts=10 \
		--online.buffer_capacity=1000 \
		--online.env_seed=10000 \
		--save_checkpoint=false \
		--save_freq=10 \
		--log_freq=1 \
		--eval.use_async_envs=true \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--device=$(DEVICE) \
		--output_dir=tests/outputs/tdmpc_online/
