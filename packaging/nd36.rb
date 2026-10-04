# Homebrew formula. See packaging/README.md for how to publish a tap.
class Nd36 < Formula
  include Language::Python::Virtualenv

  desc "Record what a program did, then replay it step by step"
  homepage "https://github.com/mehrdat/nd36"
  url "https://github.com/mehrdat/nd36/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_THE_REAL_DIGEST"
  license "MIT"

  depends_on "python@3.13"

  def install
    virtualenv_install_with_resources
  end

  test do
    (testpath/"hello.py").write <<~PYTHON
      total = 0
      for i in range(5):
          total += i
      print(total)
    PYTHON
    system bin/"nd", "run", testpath/"hello.py", "-o", testpath/"t.nd"
    assert_predicate testpath/"t.nd/events.bin", :exist?
    output = shell_output("#{bin}/nd watch #{testpath}/t.nd total")
    assert_match "total", output
  end
end
