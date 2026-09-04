# 일부러 하위 모듈을 여기서 재수출(re-export)하지 않는다. scoring.py는 app.pipeline에
# 대한 의존이 전혀 없는데, 패키지 __init__이 pipeline_adapter.py까지 한꺼번에
# import해버리면 "채점 로직만 쓰고 싶은 코드"도 app.pipeline 전체를 끌고 오게 돼서
# 애써 분리한 낮은 결합도가 무의미해진다. 필요한 모듈만 각자 직접 import한다:
#   from scripts.golden_set.scoring import FieldScorer
#   from scripts.golden_set.pipeline_adapter import NoticePipelineExtractor
