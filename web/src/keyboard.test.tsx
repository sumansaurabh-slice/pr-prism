import { fireEvent, render } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useGlobalKeys } from "./keyboard";
import { useStore } from "./store";

function Keys() {
  useGlobalKeys();
  return <textarea aria-label="message" />;
}

describe("global keys", () => {
  beforeEach(() => useStore.setState({ helpOpen: false }));

  it("toggles the shortcut list with ?", () => {
    render(<Keys />);
    fireEvent.keyDown(window, { key: "?" });
    expect(useStore.getState().helpOpen).toBe(true);
  });

  it("ignores keys typed into a text box", () => {
    const { getByLabelText } = render(<Keys />);
    fireEvent.keyDown(getByLabelText("message"), { key: "?" });
    expect(useStore.getState().helpOpen).toBe(false);
  });
});
