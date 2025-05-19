import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Type

import draccus
from huggingface_hub import hf_hub_download
from huggingface_hub.errors import HfHubHTTPError

from lerobot.common import envs
from lerobot.common.optim import OptimizerConfig
from lerobot.common.optim.schedulers import LRSchedulerConfig
from lerobot.common.utils.hub import HubMixin
from lerobot.common.utils.utils import auto_select_torch_device, is_amp_available
from lerobot.configs import parser
from lerobot.configs.default import DatasetConfig, EvalConfig, WandBConfig
from lerobot.configs.policies import PreTrainedConfig

TRAIN_CONFIG_NAME = "train_config.json"


@dataclass
class OfflineConfig:
    steps: int = 100_000


@dataclass
class OnlineConfig:
    """
    The online training loop looks something like:

    ```python
    for i in range(steps):
        do_online_rollout_and_update_online_buffer()
        for j in range(steps_between_rollouts):
            batch = next(dataloader_with_offline_and_online_data)
            loss = policy(batch)
            loss.backward()
            optimizer.step()
    ```

    Note that the online training loop adopts most of the options from the offline loop unless specified
    otherwise.
    """

    steps: int = 0
    # How many episodes to collect at once when we reach the online rollout part of the training loop.
    rollout_n_episodes: int = 1
    # The number of environments to use in the gym.vector.VectorEnv. This ends up also being the batch size for
    # the policy. Ideally you should set this to by an even divisor of rollout_n_episodes.
    rollout_batch_size: int = 1
    # How many optimization steps (forward, backward, optimizer step) to do between running rollouts.
    steps_between_rollouts: int | None = None
    # The proportion of online samples (vs offline samples) to include in the online training batches.
    sampling_ratio: float = 0.5
    # First seed to use for the online rollout environment. Seeds for subsequent rollouts are incremented by 1.
    env_seed: int | None = None
    # Sets the maximum number of frames that are stored in the online buffer for online training. The buffer is
    # FIFO.
    buffer_capacity: int | None = None
    # The minimum number of frames to have in the online buffer before commencing online training.
    # If buffer_seed_size > rollout_n_episodes, the rollout will be run multiple times until the
    # seed size condition is satisfied.
    buffer_seed_size: int = 0
    # Whether to run the online rollouts asynchronously. This means we can run the online training steps in
    # parallel with the rollouts. This might be advised if your GPU has the bandwidth to handle training
    # + eval + environment rendering simultaneously.
    do_rollout_async: bool = False

    def __post_init__(self):
        if self.steps == 0:
            return

        if self.steps_between_rollouts is None:
            raise ValueError(
                "'steps_between_rollouts' must be set to a positive integer, but it is currently None."
            )
        if self.env_seed is None:
            raise ValueError("'env_seed' must be set to a positive integer, but it is currently None.")
        if self.buffer_capacity is None:
            raise ValueError("'buffer_capacity' must be set to a positive integer, but it is currently None.")


@dataclass
class TrainPipelineConfig(HubMixin):
    dataset: DatasetConfig
    env: envs.EnvConfig | None = None
    policy: PreTrainedConfig | None = None
    # Set `dir` to where you would like to save all of the run outputs. If you run another training session
    # with the same value for `dir` its contents will be overwritten unless you set `resume` to true.
    output_dir: Path | None = None
    job_name: str | None = None
    # Set `resume` to true to resume a previous run. In order for this to work, you will need to make sure
    # `dir` is the directory of an existing run with at least one checkpoint in it.
    # Note that when resuming a run, the default behavior is to use the configuration from the checkpoint,
    # regardless of what's provided with the training command at the time of resumption.
    resume: bool = False
    device: str | None = None  # cuda | cpu | mp
    # `use_amp` determines whether to use Automatic Mixed Precision (AMP) for training and evaluation. With AMP,
    # automatic gradient scaling is used.
    use_amp: bool = False
    # `seed` is used for training (eg: model initialization, dataset shuffling)
    # AND for the evaluation environments.
    seed: int | None = 1000
    # Number of workers for the dataloader.
    num_workers: int = 4
    batch_size: int = 8
    eval_freq: int = 20_000
    log_freq: int = 200
    save_checkpoint: bool = True
    # Checkpoint is saved every `save_freq` training iterations and after the last training step.
    save_freq: int = 20_000
    offline: OfflineConfig = field(default_factory=OfflineConfig)
    online: OnlineConfig = field(default_factory=OnlineConfig)
    use_policy_training_preset: bool = True
    optimizer: OptimizerConfig | None = None
    scheduler: LRSchedulerConfig | None = None
    eval: EvalConfig = field(default_factory=EvalConfig)
    wandb: WandBConfig = field(default_factory=WandBConfig)

    def __post_init__(self):
        self.checkpoint_path = None

    def validate(self):
        if not self.device:
            logging.warning("No device specified, trying to infer device automatically")
            device = auto_select_torch_device()
            self.device = device.type

        # Automatically deactivate AMP if necessary
        if self.use_amp and not is_amp_available(self.device):
            logging.warning(
                f"Automatic Mixed Precision (amp) is not available on device '{self.device}'. Deactivating AMP."
            )
            self.use_amp = False

        # HACK: We parse again the cli args here to get the pretrained paths if there was some.
        policy_path = parser.get_path_arg("policy")
        if policy_path:
            # Only load the policy config
            cli_overrides = parser.get_cli_overrides("policy")
            self.policy = PreTrainedConfig.from_pretrained(policy_path, cli_overrides=cli_overrides)
            self.policy.pretrained_path = policy_path
        elif self.resume:
            # The entire train config is already loaded, we just need to get the checkpoint dir
            config_path = parser.parse_arg("config_path")
            if not config_path:
                raise ValueError("A config_path is expected when resuming a run.")
            policy_path = Path(config_path).parent
            self.policy.pretrained_path = policy_path
            self.checkpoint_path = policy_path.parent

        if not self.job_name:
            if self.env is None:
                self.job_name = f"{self.policy.type}"
            else:
                self.job_name = f"{self.env.type}_{self.policy.type}"

        if not self.resume and isinstance(self.output_dir, Path) and self.output_dir.is_dir():
            raise FileExistsError(
                f"Output directory {self.output_dir} alreay exists and resume is {self.resume}. "
                f"Please change your output directory so that {self.output_dir} is not overwritten."
            )
        elif not self.output_dir:
            now = dt.datetime.now()
            train_dir = f"{now:%Y-%m-%d}/{now:%H-%M-%S}_{self.job_name}"
            self.output_dir = Path("outputs/train") / train_dir

        if self.online.steps > 0:
            if isinstance(self.dataset.repo_id, list):
                raise NotImplementedError("Online training with LeRobotMultiDataset is not implemented.")
            if self.env is None:
                raise ValueError("An environment is required for online training")

        if not self.use_policy_training_preset and (self.optimizer is None or self.scheduler is None):
            raise ValueError("Optimizer and Scheduler must be set when the policy presets are not used.")
        elif self.use_policy_training_preset and not self.resume:
            self.optimizer = self.policy.get_optimizer_preset()
            self.scheduler = self.policy.get_scheduler_preset()

    @classmethod
    def __get_path_fields__(cls) -> list[str]:
        """This enables the parser to load config from the policy using `--policy.path=local/dir`"""
        return ["policy"]

    def _save_pretrained(self, save_directory: Path) -> None:
        with open(save_directory / TRAIN_CONFIG_NAME, "w") as f, draccus.config_type("json"):
            draccus.dump(self, f, indent=4)

    @classmethod
    def from_pretrained(
        cls: Type["TrainPipelineConfig"],
        pretrained_name_or_path: str | Path,
        *,
        force_download: bool = False,
        resume_download: bool = None,
        proxies: dict | None = None,
        token: str | bool | None = None,
        cache_dir: str | Path | None = None,
        local_files_only: bool = False,
        revision: str | None = None,
        **kwargs,
    ) -> "TrainPipelineConfig":
        model_id = str(pretrained_name_or_path)
        config_file: str | None = None
        if Path(model_id).is_dir():
            if TRAIN_CONFIG_NAME in os.listdir(model_id):
                config_file = os.path.join(model_id, TRAIN_CONFIG_NAME)
            else:
                print(f"{TRAIN_CONFIG_NAME} not found in {Path(model_id).resolve()}")
        elif Path(model_id).is_file():
            config_file = model_id
        else:
            try:
                config_file = hf_hub_download(
                    repo_id=model_id,
                    filename=TRAIN_CONFIG_NAME,
                    revision=revision,
                    cache_dir=cache_dir,
                    force_download=force_download,
                    proxies=proxies,
                    resume_download=resume_download,
                    token=token,
                    local_files_only=local_files_only,
                )
            except HfHubHTTPError as e:
                raise FileNotFoundError(
                    f"{TRAIN_CONFIG_NAME} not found on the HuggingFace Hub in {model_id}"
                ) from e

        cli_args = kwargs.pop("cli_args", [])
        cfg = draccus.parse(cls, config_file, args=cli_args)

        return cfg
