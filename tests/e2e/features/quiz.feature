Feature: Quiz Flow for Student
  As a student whose submission has been promoted to the quiz stage
  I want to complete a quiz associated with my assignment
  So that I can fully pass the assignment

  Background:
    Given the teacher account exists in the database
    And the E2E test subject exists
    And the student is enrolled in the E2E test subject
    And I am logged in as the student

  @quiz
  Scenario: Student starts a quiz and fails the first attempt
    Given the student's lab2 submission is in QUIZ_SENT status
    When I start the quiz for the lab2 assignment
    And I answer the quiz question incorrectly
    And I submit the quiz
    Then the quiz result page should show a failed result
    And a retry option should be available

  @quiz
  Scenario: Student retries the quiz and passes
    Given the student's lab2 submission is in QUIZ_SENT status
    And I have a failed quiz attempt
    When I start another quiz attempt
    And I answer the quiz question correctly
    And I submit the quiz
    Then the quiz result page should show a passed result
