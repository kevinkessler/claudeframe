from claudeframe.buttons import ButtonControls
from claudeframe.config import Config


class FakeButton:
    created = []

    def __init__(self, pin, pull_up, bounce_time):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.when_pressed = None
        self.closed = False
        self.created.append(self)

    def close(self):
        self.closed = True


def test_gpio_buttons_use_confirmed_bcm_mapping_and_enqueue_callbacks():
    FakeButton.created = []
    pressed = []
    controls = ButtonControls(
        lambda: pressed.append("previous"),
        lambda: pressed.append("flag"),
        lambda: pressed.append("next"),
        button_class=FakeButton,
    )

    assert controls.start() is True
    assert [button.pin for button in FakeButton.created] == [22, 27, 17]
    assert all(button.pull_up is True for button in FakeButton.created)
    assert all(button.bounce_time == 0.05 for button in FakeButton.created)

    for button in FakeButton.created:
        button.when_pressed()
    assert pressed == ["previous", "flag", "next"]

    controls.stop()
    assert all(button.closed for button in FakeButton.created)


def test_buttons_are_disabled_by_default_for_original_frame():
    assert Config().buttons_enabled is False


def test_gpio_initialization_failure_is_contained_and_cleans_up():
    created = []

    class FailingButton(FakeButton):
        def __init__(self, pin, pull_up, bounce_time):
            if pin == 27:
                raise RuntimeError("GPIO unavailable")
            super().__init__(pin, pull_up, bounce_time)
            created.append(self)

    controls = ButtonControls(lambda: None, lambda: None, lambda: None, button_class=FailingButton)

    assert controls.start() is False
    assert created[0].closed is True


def test_callback_assignment_failure_closes_constructed_button():
    created = []

    class CallbackRejectingButton:
        def __init__(self, pin, pull_up, bounce_time):
            self.pin = pin
            self.closed = False
            created.append(self)

        @property
        def when_pressed(self):
            return None

        @when_pressed.setter
        def when_pressed(self, callback):
            raise RuntimeError("callback registration failed")

        def close(self):
            self.closed = True

    controls = ButtonControls(
        lambda: None,
        lambda: None,
        lambda: None,
        button_class=CallbackRejectingButton,
    )

    assert controls.start() is False
    assert len(created) == 1
    assert created[0].closed is True
