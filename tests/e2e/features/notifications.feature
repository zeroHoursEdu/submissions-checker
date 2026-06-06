Feature: Student Notification Preferences
  As an enrolled student
  I want to configure my notification channel preferences
  So that I receive alerts through my preferred channels

  Background:
    Given the teacher account exists in the database
    And the E2E test subject exists
    And the student is enrolled in the E2E test subject
    And I am logged in as the student

  @notifications
  Scenario: Student disables email notifications for submission checked
    Given I am on the notification preferences page
    And the SUBMISSION_CHECKED email notification is currently enabled
    When I toggle the SUBMISSION_CHECKED EMAIL notification
    Then the SUBMISSION_CHECKED EMAIL notification should be disabled

  @notifications
  Scenario: Student re-enables email notifications for submission checked
    Given I am on the notification preferences page
    And the SUBMISSION_CHECKED email notification is currently disabled
    When I toggle the SUBMISSION_CHECKED EMAIL notification
    Then the SUBMISSION_CHECKED EMAIL notification should be enabled
