"""Plain side-conversation output preserves foreground streaming framing."""

from kohakuterrarium.builtins.outputs.stdout import StdoutOutput


async def test_btw_result_is_framed_without_resetting_parent_stream(capsys):
    output = StdoutOutput(prefix="Assistant: ")
    await output.write_stream("parent before")
    output.on_activity_with_metadata(
        "btw_result", "side answer", {"job_id": "side-1", "audience": "user"}
    )
    assert output._streaming
    assert output._has_output
    await output.write_stream("parent after")
    assert capsys.readouterr().out == (
        "Assistant: parent before\n[btw side-1 · side conversation]\n"
        "side answer\n[/btw]\nparent after"
    )
    output.on_activity_with_metadata("tool_start", "ignored", {})
    assert capsys.readouterr().out == ""
