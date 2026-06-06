Feature: Feedback Request and Response Flow
  As a teacher I can request end-of-semester feedback from students
  As a student I can respond to feedback requests via a token URL

  Background:
    Given the teacher account exists in the database
    And the E2E test subject exists
    And the student is enrolled in the E2E test subject
    And a semester exists in the database

  @feedback
  Scenario: Teacher requests feedback for a subject
    Given I am logged in as the teacher
    And I am on the subject page for the E2E test subject
    When I click the Request Feedback button
    Then a feedback request should be created for the subject

  @feedback
  Scenario: Student responds to feedback request via token URL
    Given a feedback request exists for the E2E test subject
    And a feedback token URL is available
    When I navigate to the feedback token URL as an anonymous user
    And I submit the feedback form with a rating of 5
    Then I should be redirected to the thank you page
