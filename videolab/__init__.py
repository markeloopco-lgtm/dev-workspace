"""動画フレーム分析 × 解説動画制作パイプライン (videolab)。

参考動画をフレーム単位で計測して「スタイルプロファイル」(数値の作風仕様)を作り、
同じプロファイルを目標値として自作動画を組み立て、出来上がりを同じ物差しで採点する。

  analyze  : 動画 → frames.csv / shots.csv / audio.json / profile.json / report.html
  aggregate: 複数動画の profile.json → 目標スタイル(configs/style_profile.yaml)
  produce  : エピソード台本YAML + 目標スタイル → 完成mp4
  compare  : 自作動画の profile と目標スタイルの差分採点
"""

__version__ = "0.1.0"
