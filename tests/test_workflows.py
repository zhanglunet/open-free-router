"""GitHub Actions 工作流文件的结构校验。

**为什么要有这个测试。**
`data-refresh.yml` 从 2026-08-06 加进来那天起就不是合法 YAML：一个多行的
`git commit -m "..."` 和一段 heredoc 正文写在了第 0 列，从 `run: |` 的块标量里逃了出去。
GitHub 对这种文件的反应是每次触发都记一条 "This run likely failed because of a
workflow file issue" 的失败——**但没有任何一条 CI 检查会因此变红**，因为坏掉的正是
那个本该跑起来的工作流自己。于是它悄悄躺了一个月，schedule 一次都没跑成，
公开目录快照冻在 2026-08-03，直到 32 天后越过 `MAX_CATALOG_AGE_DAYS`，
把**每一个不相关的 PR** 都染红——正是 data-refresh.yml 自己的注释预言的那一幕。

**为什么只验「能解析」不够。**
逃出块标量的行如果自带冒号，YAML 会把它**当成一个新的顶层键**静默收下，不报任何错。
本例里就有这么一行：`Review the diff as a data change: an `api` field ... is`。
它没触发解析错误纯属运气——前面还有一行不带冒号的先炸了。所以下面第二项
（顶层键白名单）才是真正拦得住这类事故的那一项，解析检查只是它的前提。
"""
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# GitHub Actions 允许的工作流顶层键。逃出块标量、又恰好自带冒号的正文行，
# 会以「不在这张表里的顶层键」这个形态暴露出来。
ALLOWED_TOP_LEVEL = {
    "name", "on", "run-name", "permissions", "env",
    "defaults", "concurrency", "jobs",
}


def workflow_files():
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def test_there_are_workflows_to_check():
    # 防止 glob 写错时整个测试静默地什么都没验
    assert workflow_files(), f"{WORKFLOW_DIR} 下一个工作流文件都没找到"


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_workflow_parses_as_yaml(path):
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        pytest.fail(f"{path.name} 不是合法 YAML，GitHub 会拒绝执行它：\n{exc}")


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_workflow_shape_is_a_workflow(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path.name} 顶层不是映射"

    # PyYAML 按 YAML 1.1 把裸 `on:` 读成布尔 True；GitHub 不会。
    # 这是解析器差异，不是文件的问题，这里归一化掉。
    keys = {"on" if k is True else k for k in doc}

    unexpected = keys - ALLOWED_TOP_LEVEL
    assert not unexpected, (
        f"{path.name} 出现了不该有的顶层键 {sorted(unexpected)}。"
        "最常见的成因是某一行从 `run: |` 的块标量里逃了出去——"
        "它自带冒号，于是被 YAML 当成新的顶层键静默收下。"
        "检查块内每一行的缩进是否都不小于块首行。"
    )
    assert "jobs" in keys, f"{path.name} 没有 jobs"

    jobs = doc["jobs"]
    assert isinstance(jobs, dict) and jobs, f"{path.name} 的 jobs 为空或形状不对"
    for job_name, job in jobs.items():
        assert isinstance(job, dict), f"{path.name} 的 job {job_name} 形状不对"
        assert "runs-on" in job, f"{path.name} 的 job {job_name} 没有 runs-on"
        steps = job.get("steps")
        assert isinstance(steps, list) and steps, (
            f"{path.name} 的 job {job_name} 没有 steps"
        )
        for i, step in enumerate(steps):
            assert "uses" in step or "run" in step, (
                f"{path.name} 的 job {job_name} 第 {i} 步既没有 uses 也没有 run"
            )
