Feature: Student Assignment Submission Cycle
  As an enrolled student
  I want to submit my assignment work
  So that it gets automatically checked and graded

  Background:
    Given the teacher account exists in the database
    And the E2E test subject exists
    And the student is enrolled in the E2E test subject
    And I am logged in as the student

  @smoke @submission
  Scenario: Student submits work that fails the check
    Given I navigate to the lab1 assignment
    When I upload the failing submission ZIP
    Then the submission should eventually show status FAILED

  @smoke @submission
  Scenario: Student resubmits fixed work that passes the check
    Given I navigate to the lab1 assignment
    And a previous submission has failed
    When I upload the passing submission ZIP
    Then the submission should eventually show status PASSED

  @submission @sad-path
  Scenario: Student cannot submit to a fully passed assignment (max submissions enforced)
    Given the lab1 assignment has already been passed
    When I try to upload another submission after passing
    Then the system should block the upload or redirect without creating a new submission
