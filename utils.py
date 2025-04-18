from matplotlib import pyplot as plt
import numpy as np
import scipy
import torch
import torch.nn as nn
from scipy.interpolate import CubicSpline
import parselmouth as pm
import seaborn as sns

def get_mel_fn(
        sr     : float, 
        n_fft  : int, 
        n_mels : int, 
        fmin   : float, 
        fmax   : float, 
        htk    : bool, 
        device : str = 'cpu'
) -> torch.Tensor:
    '''
    Args:
        htk: bool
            Whether to use HTK formula or Slaney formula for mel calculation'
    Returns:
        weights: Tensor [shape = (n_mels, n_fft // 2 + 1)]
    '''
    fmin = torch.tensor(fmin, device=device)
    fmax = torch.tensor(fmax, device=device)
    
    if htk:
        min_mel = 2595.0 * torch.log10(1.0 + fmin / 700.0)
        max_mel = 2595.0 * torch.log10(1.0 + fmax / 700.0)
        mels = torch.linspace(min_mel, max_mel, n_mels + 2, device=device)
        mel_f = 700.0 * (10.0 ** (mels / 2595.0) - 1.0)
    else:
        f_sp = 200.0 / 3
        min_log_hz = 1000.0
        min_log_mel = (min_log_hz) / f_sp
        logstep = torch.log(torch.tensor(6.4, device=device)) / 27.0

        if fmin >= min_log_hz:
            min_mel = min_log_mel + torch.log(fmin / min_log_hz) / logstep
        else:
            min_mel = (fmin) / f_sp

        if fmax >= min_log_hz:
            max_mel = min_log_mel + torch.log(fmax / min_log_hz) / logstep
        else:
            max_mel = (fmax) / f_sp

        mels = torch.linspace(min_mel, max_mel, n_mels + 2, device=device)
        mel_f = torch.zeros_like(mels)

        log_t = mels >= min_log_mel
        mel_f[~log_t] =f_sp * mels[~log_t]
        mel_f[log_t] = min_log_hz * torch.exp(logstep * (mels[log_t] - min_log_mel))

    n_mels = int(n_mels)
    N = 1 + n_fft // 2
    weights = torch.zeros((n_mels, N), device=device)
    
    fftfreqs = (sr / n_fft) * torch.arange(0, N, device=device)
    
    fdiff = torch.diff(mel_f)
    ramps = mel_f.unsqueeze(1) - fftfreqs.unsqueeze(0)
    
    lower = -ramps[:-2] / fdiff[:-1].unsqueeze(1)
    upper = ramps[2:] / fdiff[1:].unsqueeze(1)
    weights = torch.max(torch.tensor(0.0), torch.min(lower, upper))
    
    enorm = 2.0 / (mel_f[2 : n_mels + 2] - mel_f[:n_mels])
    weights *= enorm.unsqueeze(1)
    
    return weights

def expand_uv(uv):
    uv = uv.astype('float')
    uv = np.min(np.array([uv[:-2],uv[1:-1],uv[2:]]),axis=0)
    uv = np.pad(uv, (1, 1), constant_values=(uv[0], uv[-1]))

    return uv


def norm_f0(f0: np.ndarray, uv=None):
    if uv is None:
        uv = f0 == 0

    f0 = np.log2(f0 + uv)  # avoid arithmetic error
    f0[uv] = -np.inf

    return f0

def denorm_f0(f0: np.ndarray, uv, pitch_padding=None):
    f0 = 2 ** f0

    if uv is not None:
        f0[uv > 0] = 0
        
    if pitch_padding is not None:
        f0[pitch_padding] = 0

    return f0


def interp_f0_spline(f0: np.ndarray, uv=None):
    if uv is None:
        uv = f0 == 0
    f0max = np.max(f0)
    f0 = norm_f0(f0, uv)

    if uv.any() and not uv.all():
        spline = CubicSpline(np.where(~uv)[0], f0[~uv])
        f0[uv] = spline(np.where(uv)[0])

    return np.clip(denorm_f0(f0, uv=None),0,f0max), uv

def interp_f0(f0: np.ndarray, uv=None):
    if uv is None:
        uv = f0 == 0
    f0 = norm_f0(f0, uv)

    if uv.any() and not uv.all():
        f0[uv] = np.interp(np.where(uv)[0], np.where(~uv)[0], f0[~uv])

    return denorm_f0(f0, uv=None), uv
    

def get_n_fft(f0: torch.Tensor, sr: int, relative_winsize: int):
    '''
    Args:
        f0: Tensor [shape = (n_frames)]
        relative_winsize : int
            Relative window size in seconds
    Returns:
        n_fft: int
    '''
    # 去掉f0小于20hz的部分,有时候f0会出现很小的数值,导致n_fft计算错误
    f0 = f0[f0 > 20]
    f0_min = f0.min()
    if f0_min > 1000:
        f0_min = torch.tensor(1000)
        
    max_winsize = torch.round(sr / f0_min * relative_winsize / 2) * 2
    n_fft = 2 ** torch.ceil(torch.log2(max_winsize))

    return n_fft.int(), f0_min

def upsample(signal, factor):
    '''
        signal: B x C X T
        factor: int
        return: B x C X T*factor
    '''
    signal = nn.functional.interpolate(
        torch.cat(
            (signal,signal[:,:,-1:]),
            2
        ), 
        size=signal.shape[-1] * factor + 1, 
        mode='linear', 
        align_corners=True
    )
    signal = signal[:,:,:-1]
    return signal

class DotDict(dict):
    def __getattr__(self, attr):
        return self.get(attr)
    def __setattr__(self, key, value):
        self[key] = value

def analyze_model_parameters(model):
    # 存储所有参数的统计信息
    param_stats = {}

    for name, param in model.named_parameters():
        if param.requires_grad:
            param_data = param.data.cpu().numpy().flatten()  # 展平为一维数组
            
            # 计算统计特征
            stats = {
                "mean": np.mean(param_data),
                "std": np.std(param_data),
                "min": np.min(param_data),
                "max": np.max(param_data),
                "median": np.median(param_data),
                "skewness": scipy.stats.skew(param_data),
                "kurtosis": scipy.stats.kurtosis(param_data),
                "q1": np.percentile(param_data, 25),
                "q3": np.percentile(param_data, 75),
                "90th_percentile": np.percentile(param_data, 90),
                "99th_percentile": np.percentile(param_data, 99),
            }
            
            param_stats[name] = stats

            # 打印统计信息
            print(f"Parameter: {name}")
            for key, value in stats.items():
                print(f"  {key}: {value:.4f}")

            # 绘制直方图和密度估计图
            plt.figure(figsize=(10, 4))
            sns.histplot(param_data, kde=True, bins=50, color="blue")
            plt.title(f"Distribution of Parameter: {name}")
            plt.xlabel("Value")
            plt.ylabel("Density")
            plt.show()

    return param_stats

def extract_f0_parselmouth(config, x: np.ndarray, n_frames):
    l_pad = int(
            np.ceil(
                1.5 / config.f0_min * config.sampling_rate
            )
    )
    r_pad = config.block_size * ((len(x) - 1) // config.block_size + 1) - len(x) + l_pad + 1
    padded_signal = np.pad(x, (l_pad, r_pad))
    
    sound = pm.Sound(padded_signal, config.sampling_rate)
    pitch = sound.to_pitch_ac(
        time_step=config.block_size / config.sampling_rate, 
        voicing_threshold=0.6,
        pitch_floor=config.f0_min, 
        pitch_ceiling=1100
    )
    
    f0 = pitch.selected_array['frequency']
    if len(f0) < n_frames:
        f0 = np.pad(f0, (0, n_frames - len(f0)))
    f0 = f0[:n_frames]

    return f0

if __name__ == '__main__':
    # test
    '''librosa_mel = librosa_mel_fn(sr=44100, n_fft=2048, n_mels=128, fmin=20, fmax=22050, htk=False)
    custom_mel = get_mel_fn(sr=44100, n_fft=2048, n_mels=128, fmin=20, fmax=22050, htk=False, device='cpu').to('cpu')
    print(torch.allclose(torch.tensor(librosa_mel), custom_mel, atol=1e-5))
    print(np.max( np.abs(librosa_mel - custom_mel.numpy()) ))
    # 画出mel filter的对比图，以及相减后的结果
    plt.figure(figsize=(50, 5))
    plt.subplot(3, 1, 1)
    plt.imshow(librosa_mel, origin='lower')
    plt.title('librosa_mel')
    plt.subplot(3, 1, 2)
    plt.imshow(custom_mel.numpy(), origin='lower')
    plt.title('custom_mel')
    plt.subplot(3, 1, 3)
    plt.imshow(np.abs(librosa_mel - custom_mel.numpy()), origin='lower')
    plt.title('diff')
    plt.show()'''

    # test get_n_fft
    f0 = torch.tensor([2000], dtype=torch.float32)
    sr = 44100
    relative_winsize = 4
    n_fft = get_n_fft(f0, sr, relative_winsize)
    print(n_fft)
