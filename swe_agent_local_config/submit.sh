
# @yaml
# signature: submit
# docstring: submits your current code and terminates the session
submit() {
    cd $ROOT

    # Discard any modifications to test files so they never leak into the
    # submitted patch.
    git checkout -- testing/ tests/ 2>/dev/null || true

    git add -u
    git diff --cached > $ROOT/model.patch
    echo "<<SUBMISSION||"
    cat $ROOT/model.patch
    echo "||SUBMISSION>>"
}
